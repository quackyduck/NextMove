"""Wording of the branded HTML emails, one entry per kind (rendered by mailer.render into templates/email/).

The plain-text part of each email is built where it is sent (confirmations, password_reset, deletion) and is
sent unchanged; this module only shapes the HTML part. Everything here is fixed copy. The only values filled
in are the masked address, a server-built link and fixed facts (a time, a number of minutes, a date), and
the template autoescapes all of them. `{support}` is the configured reply-to address (support_address()). `button` and `secondary` name either 'link' (the server-built token
link) or a site path, which mailer.public_url turns into an absolute https URL.
"""

EMAILS = {
    'signup': dict(
        preheader='One click to finish creating your account. The link expires in 24 hours.',
        title='Confirm your email',
        paragraphs=['Someone, hopefully you, asked to create a My Next Moov account with this address. '
                    'Confirm it to finish creating your account.'],
        button=('Create my account', 'link'),
        note="The link works once and expires in 24 hours. If this wasn't you, ignore this email; "
             'no account is created.',
        why='You got this because someone signed up for My Next Moov with {masked}.',
    ),
    'account_exists': dict(
        preheader='Sign in, or reset your password if you have forgotten it.',
        title='You already have an account',
        paragraphs=['Someone, hopefully you, tried to sign up with this address, but it already has a '
                    'My Next Moov account.'],
        button=('Sign in', '/?signin'),
        secondary=('Forgot your password?', 'Reset it', '/?forgot'),
        note="If this wasn't you, you can ignore this email. Nothing about your account has changed.",
        why='You got this because someone tried to sign up for My Next Moov with {masked}.',
    ),
    'activation': dict(
        preheader='Confirm this address to open the dashboard for your chat account.',
        title='Open your dashboard',
        paragraphs=['Someone, hopefully you, asked to use this address for a My Next Moov account created in chat. '
                    'Confirm it to open your dashboard. Your resumes, roles and drafts from chat are already there.'],
        button=('Open my dashboard', 'link'),
        note="The link works once and expires in 24 hours. If this wasn't you, ignore this email; nothing changes.",
        why='You got this because someone chose {masked} for a My Next Moov chat account.',
    ),
    'activation_exists': dict(
        preheader='This address already belongs to a My Next Moov account.',
        title='This address already has an account',
        paragraphs=['Someone, hopefully you, asked to use this address for a My Next Moov account created in chat, '
                    'but it already belongs to another account.',
                    'To use that account from chat, contact support and ask to link your phone.'],
        note="If this wasn't you, you can ignore this email.",
        why='You got this because someone chose {masked} for a My Next Moov chat account.',
    ),
    'existing': dict(
        preheader='Confirm your address to finish signing in.',
        title='Confirm your email',
        paragraphs=['Someone, hopefully you, signed in to My Next Moov. To keep your account secure, '
                    'confirm that this address is yours.'],
        button=('Confirm and sign in', 'link'),
        secondary=("Didn't sign in?", 'Reset your password', '/?forgot'),
        note='The link works once and expires in 24 hours.',
        why='You got this because someone signed in to the My Next Moov account for {masked}.',
    ),
    'password_reset': dict(
        preheader='Choose a new password. The link expires in {minutes} minutes.',
        title='Reset your password',
        paragraphs=['Someone, hopefully you, asked to reset the password for your My Next Moov account. '
                    'Choose a new one with the link below.'],
        button=('Choose a new password', 'link'),
        note="The link works once and expires in {minutes} minutes. If you didn't ask for this, ignore this "
             "email; your password hasn't changed.",
        why='You got this because a password reset was requested for {masked}.',
    ),
    'password_changed': dict(
        preheader="Your password was changed. If this wasn't you, reset it now.",
        title='Your password was changed',
        paragraphs=['The password for your My Next Moov account was changed at {at} (UTC). '
                    'Every browser was signed out.'],
        button=('Reset my password', '/?forgot'),
        note="If this was you, there's nothing to do. If it wasn't, reset your password now and email "
             '{support}.',
        why='You got this because the password for {masked} changed. We always send this notice.',
    ),
    'account_deleted': dict(
        preheader='Your account and everything in it have been deleted.',
        title='Your account was deleted',
        paragraphs=['We deleted your My Next Moov account and everything in it on {date} (UTC).',
                    'Backups are replaced within 4 weeks; until then, a backup can still contain this data.'],
        note="Questions, or didn't ask for this? Reply to this email or write to {support}.",
        why='You got this because the My Next Moov account for {masked} was deleted.',
    ),
}
