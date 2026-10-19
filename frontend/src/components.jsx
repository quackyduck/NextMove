import React, { useId } from "react";
import { label, stages, outcomes } from "./workflow.js";

export function Field({
  name,
  title,
  values,
  set,
  area = false,
  options,
  type = "text",
  required = false,
}) {
  const id = `field-${name}-${useId()}`;
  return (
    <div className={area ? "col-12" : "col-md-6"}>
      <label className="form-label" htmlFor={id}>
        {title}
      </label>
      {options ? (
        <select
          id={id}
          className="form-select"
          value={values[name] || ""}
          onChange={(e) => set({ ...values, [name]: e.target.value })}
        >
          {options.map((v) => (
            <option
              key={Array.isArray(v) ? v[0] : v}
              value={Array.isArray(v) ? v[0] : v}
            >
              {Array.isArray(v) ? v[1] : label(v)}
            </option>
          ))}
        </select>
      ) : area ? (
        <textarea
          id={id}
          rows={name === "description" ? 5 : 3}
          className="form-control"
          value={values[name] || ""}
          onChange={(e) => set({ ...values, [name]: e.target.value })}
        />
      ) : (
        <input
          id={id}
          type={type}
          required={required}
          className="form-control"
          value={values[name] || ""}
          onChange={(e) => set({ ...values, [name]: e.target.value })}
        />
      )}
    </div>
  );
}
export function JobFields({ values, set, detail = false }) {
  return (
    <div className="row g-3">
      <Field name="title" title="Role title" values={values} set={set} />
      <Field name="company" title="Company" values={values} set={set} />
      <Field
        name="url"
        title="Job posting URL"
        type="url"
        values={values}
        set={set}
      />
      <Field
        name="found_date"
        title="Date found"
        type="date"
        values={values}
        set={set}
      />
      <Field name="location" title="Location" values={values} set={set} />
      <Field
        name="salary"
        title="Salary / compensation"
        values={values}
        set={set}
      />
      <Field name="lane" title="Category / lane" values={values} set={set} />
      <Field
        name="priority"
        title="Priority"
        options={["low", "medium", "high"]}
        values={values}
        set={set}
      />
      <Field
        name="description"
        title="Job description (paste here)"
        area
        values={values}
        set={set}
      />
      {detail && (
        <>
          <Field
            name="stage"
            title="Stage"
            options={stages}
            values={values}
            set={set}
          />
          <Field
            name="outcome"
            title="Outcome"
            options={outcomes}
            values={values}
            set={set}
          />
        </>
      )}
      <Field name="next_action" title="Next action" values={values} set={set} />
      <Field
        name="next_action_date"
        title="Next action date"
        type="date"
        values={values}
        set={set}
      />
      <Field name="notes" title="Your notes" area values={values} set={set} />
    </div>
  );
}
