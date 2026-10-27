"""Onboarding step registry and per-user progress.

Steps are registered in code with an ID and version. A missing row reads as
pending, so registering a new step shows it to existing users as a checklist
item rather than re-running onboarding. Steps with a `completed` fact are done
when the fact holds (a resume exists, a token connected), never by assertion.

The guided, full-screen flow only runs for people who signed up through the
app: signup writes a `create_account` row, and the flow continues while a
guided step has no row of its own. Migrated or administrator-created accounts
get the checklist only. Bumping a step's version re-offers a skipped step on
the checklist without restarting the guided flow.
"""
from dataclasses import dataclass
from typing import Callable, Optional

from sqlalchemy import exists, select
from sqlalchemy.dialects.sqlite import insert

from .db import get_session
from .models import AgentToken, MasterResume, OnboardingStep, now
from .services import ServiceError, transaction


def has_resume(user_id):
    return get_session().scalar(select(exists().where(MasterResume.user_id == user_id)))


def has_connected_agent(user_id):
    # last_used_at is set by a successful token request (including the connection
    # test) and survives revocation, so revoking a token later keeps this done.
    return get_session().scalar(select(exists().where(
        AgentToken.user_id == user_id, AgentToken.last_used_at.is_not(None))))


@dataclass(frozen=True)
class Step:
    id: str
    version: int
    title: str
    guided: bool = False
    skippable: bool = True
    completed: Optional[Callable] = None


REGISTRY = (
    Step('create_account', 1, 'Create your account', skippable=False, completed=lambda user_id: True),
    Step('upload_resume', 1, 'Upload your resume', guided=True, completed=has_resume),
    Step('connect_agent', 1, 'Connect an agent', guided=True, completed=has_connected_agent),
)
STEPS = {step.id: step for step in REGISTRY}


def _status(step, row, user_id):
    if step.completed and step.completed(user_id):
        return 'done'
    if row is None or (row.status == 'skipped' and row.version < step.version):
        return 'pending'
    return row.status


def state(user_id):
    rows = {row.step_id: row for row in get_session().scalars(
        select(OnboardingStep).where(OnboardingStep.user_id == user_id))}
    steps = [{'id': step.id, 'version': step.version, 'title': step.title,
              'status': _status(step, rows.get(step.id), user_id),
              'guided': step.guided, 'skippable': step.skippable} for step in REGISTRY]
    # The guided flow walks guided steps the user has not acted on yet, in
    # registry order; re-offered (version-bumped) steps stay on the checklist.
    remaining = [item['id'] for item in steps
                 if item['guided'] and item['status'] == 'pending' and item['id'] not in rows]
    guided = 'create_account' in rows and bool(remaining)
    return {'steps': steps, 'guided_active': guided, 'next_step': remaining[0] if guided else None}


def _record(user_id, step_id, status):
    statement = insert(OnboardingStep).values(
        user_id=user_id, step_id=step_id, version=STEPS[step_id].version, status=status, updated_at=now())
    get_session().execute(statement.on_conflict_do_update(
        index_elements=['user_id', 'step_id'],
        set_={key: statement.excluded[key] for key in ('version', 'status', 'updated_at')}))


def start(connection, user_id):
    """Run inside the signup transaction: marks this account for the guided flow."""
    connection.execute(insert(OnboardingStep).values(
        user_id=user_id, step_id='create_account', version=STEPS['create_account'].version,
        status='done', updated_at=now()))


@transaction
def set_status(user_id, step_id, data):
    if not isinstance(data, dict) or set(data) != {'status'}:
        raise ServiceError('Provide only a status')
    step = STEPS.get(step_id)
    if step is None:
        raise ServiceError('Onboarding step not found', 404)
    status = data['status']
    if status not in ('done', 'skipped'):
        raise ServiceError('Status must be done or skipped')
    finished = bool(step.completed and step.completed(user_id))
    if status == 'skipped':
        if not step.skippable:
            raise ServiceError('This step cannot be skipped')
        if not finished:
            _record(user_id, step_id, 'skipped')
    elif step.completed and not finished:
        raise ServiceError('Finish this step first; it is marked done automatically', 409)
    elif not step.completed:
        _record(user_id, step_id, 'done')
    get_session().flush()
    return state(user_id)
