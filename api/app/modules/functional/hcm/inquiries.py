"""
HCM / Payroll read-only inquiry catalog.

Each inquiry is a *fixed, server-defined* parameterized SELECT against the HR/PAY
schema plus a deterministic simulator that returns realistic synthetic rows when
no real database connection is available (mirrors the Performance agent's seeded
simulators). The SQL is SELECT-only and uses bind variables for every user input,
so there is no free-form SQL, no injection surface, and no possibility of DML.

HCM ``_F`` tables are DateTracked, so queries filter
``:as_of BETWEEN effective_start_date AND effective_end_date`` (the runner always
supplies ``as_of``, defaulting to today). Sensitive PII columns (national
identifier, date of birth, bank account, salary amounts) are intentionally NOT
selected.
"""
import random
from datetime import date, timedelta


# ── Simulators (deterministic per environment + params) ─────────────────────────

def _rng(env: str, key: str, params: dict) -> random.Random:
    seed = f"{env}|{key}|" + "|".join(f"{k}={params.get(k)}" for k in sorted(params))
    return random.Random(hash(seed) % (10 ** 9))


_ORGS = ["Vision Corporation", "Vision Operations", "Vision Services",
         "Vision Manufacturing", "Vision Finance", "Vision HR Shared Services"]
_POSITIONS = ["Staff Accountant", "Senior Analyst", "HR Generalist", "Payroll Specialist",
              "Operations Manager", "Software Engineer", "Procurement Buyer", "Team Lead"]
_GRADES = ["G1", "G2", "G3", "G4", "M1", "M2"]
_ASSIGN_STATUS = ["Active Assignment", "Active Assignment", "Active Assignment",
                  "Suspend Assignment", "Terminate Assignment"]
_PAYROLLS = ["US Semi-Monthly", "US Monthly Salaried", "UK Monthly", "Hourly Weekly"]
_ELEMENTS = [
    ("Regular Salary", "Earnings", "Recurring"),
    ("Overtime", "Earnings", "Nonrecurring"),
    ("Health Insurance", "Pre-Tax Deductions", "Recurring"),
    ("401k Contribution", "Pre-Tax Deductions", "Recurring"),
    ("Federal Tax", "Tax Deductions", "Recurring"),
    ("Bonus", "Earnings", "Nonrecurring"),
]
_ACTION_TYPES = [("R", "Payroll Run"), ("P", "PrePayments"), ("C", "Costing"), ("M", "Magnetic Transfer")]
_ACTION_STATUS = ["C", "C", "C", "I", "E"]  # Complete / Incomplete / Error


def _sim_employee_assignment(env: str, params: dict) -> list:
    rng = _rng(env, "emp", params)
    emp_no = (params.get("employee_number") or "").strip()
    count = 1 if emp_no else rng.randint(3, 6)
    rows = []
    for i in range(count):
        num = emp_no or str(rng.randint(10000, 99999))
        first = rng.choice(["Alex", "Jordan", "Priya", "Wei", "Maria", "Sam", "Nora", "Diego"])
        last = rng.choice(["Smith", "Patel", "Chen", "Garcia", "Khan", "Brown", "Okafor"])
        rows.append({
            "employee_number": num,
            "full_name": f"{last}, {first}",
            "assignment_status": rng.choice(_ASSIGN_STATUS),
            "org_name": rng.choice(_ORGS),
            "position_name": rng.choice(_POSITIONS),
            "grade": rng.choice(_GRADES),
            "supervisor": f"{rng.choice(['Lee','Davis','Mehta','Park'])}, {rng.choice(['Pat','Robin','Asha'])}",
            "hire_date": (date.today() - timedelta(days=rng.randint(120, 3650))).isoformat(),
        })
    return rows


def _sim_org_position(env: str, params: dict) -> list:
    rng = _rng(env, "org", params)
    flt = (params.get("org_name") or "").strip().lower()
    orgs = [o for o in _ORGS if flt in o.lower()] or _ORGS
    rows = []
    for org in orgs:
        for pos in rng.sample(_POSITIONS, rng.randint(2, 5)):
            rows.append({
                "org_name": org,
                "position_name": pos,
                "headcount": rng.randint(1, 25),
                "date_from": (date.today() - timedelta(days=rng.randint(400, 3000))).isoformat(),
            })
    return rows


def _sim_payroll_run_status(env: str, params: dict) -> list:
    rng = _rng(env, "run", params)
    flt = (params.get("payroll_name") or "").strip().lower()
    payrolls = [p for p in _PAYROLLS if flt in p.lower()] or _PAYROLLS
    days_back = int(params.get("days_back") or 30)
    rows = []
    for _ in range(rng.randint(4, 9)):
        total = rng.randint(50, 4000)
        errored = rng.choice([0, 0, 0, rng.randint(1, 40)])
        code, label = rng.choice(_ACTION_TYPES)
        rows.append({
            "payroll_name": rng.choice(payrolls),
            "action_type": label,
            "effective_date": (date.today() - timedelta(days=rng.randint(0, days_back))).isoformat(),
            "action_status": rng.choice(_ACTION_STATUS),
            "total_actions": total,
            "errored_actions": errored,
        })
    return sorted(rows, key=lambda r: r["effective_date"], reverse=True)


def _sim_element_entries(env: str, params: dict) -> list:
    rng = _rng(env, "ele", params)
    rows = []
    for name, classification, etype in rng.sample(_ELEMENTS, rng.randint(3, len(_ELEMENTS))):
        rows.append({
            "element_name": name,
            "classification": classification,
            "entry_type": etype,
            "effective_start_date": (date.today() - timedelta(days=rng.randint(30, 900))).isoformat(),
            "effective_end_date": "4712-12-31",
        })
    return rows


def _sim_payroll_setup(env: str, params: dict) -> list:
    rng = _rng(env, "setup", params)
    flt = (params.get("payroll_name") or "").strip().lower()
    payrolls = [p for p in _PAYROLLS if flt in p.lower()] or _PAYROLLS
    rows = []
    for p in payrolls:
        ptype = "Semi-Month" if "Semi" in p else ("Calendar Month" if "Monthly" in p else "Week")
        for k in range(rng.randint(2, 4)):
            start = date.today() - timedelta(days=rng.randint(0, 60) + k * 14)
            rows.append({
                "payroll_name": p,
                "period_type": ptype,
                "period_name": start.strftime("%b %Y") + f" P{k+1}",
                "start_date": start.isoformat(),
                "end_date": (start + timedelta(days=13)).isoformat(),
            })
    return rows


# ── Catalog ─────────────────────────────────────────────────────────────────────

INQUIRIES = [
    {
        "id": "employee_assignment",
        "label": "Employee / Assignment",
        "description": "Look up an employee's primary assignment: status, organization, "
                       "position, grade, supervisor and hire date (as of a date).",
        "params": [
            {"name": "employee_number", "label": "Employee Number", "required": False,
             "help": "Leave blank to list a sample of employees."},
        ],
        "sql": """
            SELECT papf.employee_number,
                   papf.full_name,
                   pus.user_status              AS assignment_status,
                   hou.name                     AS org_name,
                   pos.name                     AS position_name,
                   pg.name                      AS grade,
                   sup.full_name                AS supervisor,
                   papf.original_date_of_hire   AS hire_date
            FROM   per_all_people_f papf
                   JOIN per_all_assignments_f paaf
                     ON paaf.person_id = papf.person_id
                    AND :as_of BETWEEN paaf.effective_start_date AND paaf.effective_end_date
                    AND paaf.primary_flag = 'Y'
                   LEFT JOIN hr_all_organization_units hou
                     ON hou.organization_id = paaf.organization_id
                   LEFT JOIN per_all_positions pos
                     ON pos.position_id = paaf.position_id
                   LEFT JOIN per_grades pg
                     ON pg.grade_id = paaf.grade_id
                   LEFT JOIN per_assignment_status_types pus
                     ON pus.assignment_status_type_id = paaf.assignment_status_type_id
                   LEFT JOIN per_all_people_f sup
                     ON sup.person_id = paaf.supervisor_id
                    AND :as_of BETWEEN sup.effective_start_date AND sup.effective_end_date
            WHERE  :as_of BETWEEN papf.effective_start_date AND papf.effective_end_date
            AND    (:employee_number IS NULL
                    OR papf.employee_number = :employee_number)
            ORDER  BY papf.employee_number
            FETCH FIRST 100 ROWS ONLY
        """,
        "simulate": _sim_employee_assignment,
    },
    {
        "id": "org_position",
        "label": "Organization / Position",
        "description": "List positions and headcount within an organization "
                       "(optionally filtered by organization name).",
        "params": [
            {"name": "org_name", "label": "Organization (partial)", "required": False},
        ],
        "sql": """
            SELECT hou.name                          AS org_name,
                   pos.name                          AS position_name,
                   COUNT(paaf.assignment_id)         AS headcount,
                   pos.date_effective                AS date_from
            FROM   hr_all_organization_units hou
                   JOIN per_all_positions pos
                     ON pos.organization_id = hou.organization_id
                   LEFT JOIN per_all_assignments_f paaf
                     ON paaf.position_id = pos.position_id
                    AND :as_of BETWEEN paaf.effective_start_date AND paaf.effective_end_date
            WHERE  (:org_name IS NULL
                    OR UPPER(hou.name) LIKE '%' || UPPER(:org_name) || '%')
            GROUP  BY hou.name, pos.name, pos.date_effective
            ORDER  BY hou.name, pos.name
            FETCH FIRST 200 ROWS ONLY
        """,
        "simulate": _sim_org_position,
    },
    {
        "id": "payroll_run_status",
        "label": "Payroll Run Status",
        "description": "Recent payroll process actions (run / prepayments / costing) with "
                       "completed vs. errored assignment-action counts — surfaces retry candidates.",
        "params": [
            {"name": "payroll_name", "label": "Payroll (partial)", "required": False},
            {"name": "days_back", "label": "Days back", "required": False, "default": 30},
        ],
        "sql": """
            SELECT pap.payroll_name,
                   ppa.action_type,
                   ppa.effective_date,
                   ppa.action_status,
                   (SELECT COUNT(*) FROM pay_assignment_actions paa
                     WHERE paa.payroll_action_id = ppa.payroll_action_id) AS total_actions,
                   (SELECT COUNT(*) FROM pay_assignment_actions paa
                     WHERE paa.payroll_action_id = ppa.payroll_action_id
                       AND paa.action_status = 'E')                       AS errored_actions
            FROM   pay_payroll_actions ppa
                   JOIN pay_all_payrolls_f pap
                     ON pap.payroll_id = ppa.payroll_id
                    AND :as_of BETWEEN pap.effective_start_date AND pap.effective_end_date
            WHERE  ppa.effective_date >= TRUNC(SYSDATE) - :days_back
            AND    (:payroll_name IS NULL
                    OR UPPER(pap.payroll_name) LIKE '%' || UPPER(:payroll_name) || '%')
            ORDER  BY ppa.effective_date DESC
            FETCH FIRST 100 ROWS ONLY
        """,
        "simulate": _sim_payroll_run_status,
    },
    {
        "id": "element_entries",
        "label": "Element Entries",
        "description": "Element entries for an assignment as of a date (element name, "
                       "classification, recurring/non-recurring). Monetary values are not shown.",
        "params": [
            {"name": "assignment_id", "label": "Assignment ID", "required": True},
        ],
        "sql": """
            SELECT pet.element_name,
                   pec.classification_name        AS classification,
                   pee.entry_type,
                   pee.effective_start_date,
                   pee.effective_end_date
            FROM   pay_element_entries_f pee
                   JOIN pay_element_types_f pet
                     ON pet.element_type_id = pee.element_type_id
                    AND :as_of BETWEEN pet.effective_start_date AND pet.effective_end_date
                   LEFT JOIN pay_element_classifications pec
                     ON pec.classification_id = pet.classification_id
            WHERE  pee.assignment_id = :assignment_id
            AND    :as_of BETWEEN pee.effective_start_date AND pee.effective_end_date
            ORDER  BY pec.classification_name, pet.element_name
            FETCH FIRST 200 ROWS ONLY
        """,
        "simulate": _sim_element_entries,
    },
    {
        "id": "payroll_setup",
        "label": "Payroll Setup / Periods",
        "description": "Defined payrolls and their recent/open time periods.",
        "params": [
            {"name": "payroll_name", "label": "Payroll (partial)", "required": False},
        ],
        "sql": """
            SELECT pap.payroll_name,
                   pap.period_type,
                   ptp.period_name,
                   ptp.start_date,
                   ptp.end_date
            FROM   pay_all_payrolls_f pap
                   JOIN per_time_periods ptp
                     ON ptp.payroll_id = pap.payroll_id
            WHERE  :as_of BETWEEN pap.effective_start_date AND pap.effective_end_date
            AND    ptp.end_date >= TRUNC(SYSDATE) - 60
            AND    (:payroll_name IS NULL
                    OR UPPER(pap.payroll_name) LIKE '%' || UPPER(:payroll_name) || '%')
            ORDER  BY pap.payroll_name, ptp.start_date DESC
            FETCH FIRST 200 ROWS ONLY
        """,
        "simulate": _sim_payroll_setup,
    },
]

_BY_ID = {q["id"]: q for q in INQUIRIES}


def catalog() -> list:
    """Public metadata for the UI — no SQL or simulator internals."""
    return [
        {"id": q["id"], "label": q["label"], "description": q["description"], "params": q["params"]}
        for q in INQUIRIES
    ]


def get(inquiry_id: str) -> dict | None:
    return _BY_ID.get(inquiry_id)


def simulate(inquiry_id: str, env: str, params: dict) -> list:
    q = _BY_ID.get(inquiry_id)
    return q["simulate"](env or "EBS", params or {}) if q else []


def normalize_params(inquiry: dict, params: dict) -> dict:
    """Apply declared defaults and surface missing required params."""
    params = dict(params or {})
    out = {}
    missing = []
    for p in inquiry["params"]:
        val = params.get(p["name"])
        if val in (None, ""):
            val = p.get("default")
        if (val in (None, "")) and p.get("required"):
            missing.append(p["name"])
        out[p["name"]] = val if val not in ("",) else None
    if missing:
        raise ValueError(f"Missing required parameter(s): {', '.join(missing)}")
    return out
