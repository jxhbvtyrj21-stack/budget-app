"""Міграція 1: початкова схема першого релізу.

Схема відображає лише затверджені сутності (ADR 0002–0022). Перевірки ``CHECK`` і
зовнішні ключі захищають цілісність окремого рядка; правила, що залежать від інших
записів (достатність залишку, поточний місяць, архівування), виконує сервісний шар.
Місяць зберігається як текст ``YYYY-MM`` — значення, а не окрема сутність.
"""

VERSION = 1

_MONTH = "{col} GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]' AND CAST(substr({col}, 6, 2) AS INTEGER) BETWEEN 1 AND 12"  # noqa: E501
_NAME = "length(trim(name)) > 0"


def _month(col: str) -> str:
    return _MONTH.format(col=col)


SQL = f"""
-- Первинне налаштування: один рядок. Чернетка майстра не є фінансовим записом (Q176).
CREATE TABLE setup_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    status TEXT NOT NULL CHECK (status IN ('not_started', 'in_progress', 'completed')),
    draft_json TEXT,
    completed_month TEXT CHECK (completed_month IS NULL OR ({_month("completed_month")})),
    CHECK ((status = 'completed') = (completed_month IS NOT NULL)),
    CHECK (status = 'in_progress' OR draft_json IS NULL)
) STRICT;
INSERT INTO setup_state (id, status) VALUES (1, 'not_started');

-- Загальний нерозподілений залишок: підтримуваний залишок, походження не зберігається (Q167).
CREATE TABLE general_remainder (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    balance INTEGER NOT NULL CHECK (balance >= 0)
) STRICT;
INSERT INTO general_remainder (id, balance) VALUES (1, 0);

-- Дохід: незмінний; залишок похідний від пов'язаних операцій (ADR 0009).
CREATE TABLE incomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    month TEXT NOT NULL CHECK ({_month("month")}),
    name TEXT NOT NULL CHECK ({_NAME}),
    description TEXT,
    amount INTEGER NOT NULL CHECK (amount > 0),
    archived INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0, 1))
) STRICT;
CREATE INDEX incomes_month ON incomes (month);

-- Накопичення: статус і архівність — окремі виміри; архів лише для закритого (Q186).
CREATE TABLE accumulations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL CHECK ({_NAME}),
    description TEXT,
    target INTEGER CHECK (target IS NULL OR target >= 0),
    status TEXT NOT NULL CHECK (status IN ('active', 'reached', 'closed')),
    archived INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0, 1)),
    initial_balance INTEGER NOT NULL DEFAULT 0 CHECK (initial_balance >= 0),
    CHECK (archived = 0 OR status = 'closed')
) STRICT;

-- Звичайна витрата: рівно одне джерело (ADR 0003), зокрема накопичення (Q170).
CREATE TABLE expenses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    month TEXT NOT NULL CHECK ({_month("month")}),
    name TEXT NOT NULL CHECK ({_NAME}),
    description TEXT,
    amount INTEGER NOT NULL CHECK (amount > 0),
    source_kind TEXT NOT NULL
        CHECK (source_kind IN ('income', 'general_remainder', 'accumulation')),
    source_income_id INTEGER REFERENCES incomes (id) ON DELETE RESTRICT,
    source_accumulation_id INTEGER REFERENCES accumulations (id) ON DELETE RESTRICT,
    CHECK (
        (source_kind = 'income' AND source_income_id IS NOT NULL AND source_accumulation_id IS NULL)
        OR (source_kind = 'general_remainder'
            AND source_income_id IS NULL AND source_accumulation_id IS NULL)
        OR (source_kind = 'accumulation'
            AND source_income_id IS NULL AND source_accumulation_id IS NOT NULL)
    )
) STRICT;
CREATE INDEX expenses_month ON expenses (month);
CREATE INDEX expenses_source_income ON expenses (source_income_id);
CREATE INDEX expenses_source_accumulation ON expenses (source_accumulation_id);

-- Поповнення: одна операція одного місяця з однією чи кількома частинами (Q156).
CREATE TABLE replenishments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    month TEXT NOT NULL CHECK ({_month("month")}),
    name TEXT NOT NULL CHECK ({_NAME}),
    description TEXT,
    accumulation_id INTEGER NOT NULL REFERENCES accumulations (id) ON DELETE RESTRICT
) STRICT;
CREATE INDEX replenishments_month ON replenishments (month);
CREATE INDEX replenishments_accumulation ON replenishments (accumulation_id);

-- Частина поповнення: джерело — дохід або загальний нерозподілений залишок (ADR 0007).
CREATE TABLE replenishment_parts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    replenishment_id INTEGER NOT NULL REFERENCES replenishments (id) ON DELETE CASCADE,
    source_kind TEXT NOT NULL CHECK (source_kind IN ('income', 'general_remainder')),
    source_income_id INTEGER REFERENCES incomes (id) ON DELETE RESTRICT,
    amount INTEGER NOT NULL CHECK (amount > 0),
    CHECK ((source_kind = 'income') = (source_income_id IS NOT NULL))
) STRICT;
CREATE INDEX replenishment_parts_replenishment ON replenishment_parts (replenishment_id);
CREATE INDEX replenishment_parts_source_income ON replenishment_parts (source_income_id);

-- Борг: початковий (без місяця, не поповнює нерозподілений залишок)
-- або створений отриманням позикових коштів (ADR 0018, ADR 0022).
CREATE TABLE debts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    origin TEXT NOT NULL CHECK (origin IN ('initial', 'loan_receipt')),
    month TEXT CHECK (month IS NULL OR ({_month("month")})),
    name TEXT NOT NULL CHECK ({_NAME}),
    description TEXT,
    amount INTEGER NOT NULL CHECK (amount > 0),
    CHECK ((origin = 'initial') = (month IS NULL))
) STRICT;
CREATE INDEX debts_month ON debts (month);

-- Погашення: одне джерело; не є звичайною витратою (ADR 0018).
CREATE TABLE debt_repayments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    debt_id INTEGER NOT NULL REFERENCES debts (id) ON DELETE RESTRICT,
    month TEXT NOT NULL CHECK ({_month("month")}),
    description TEXT,
    amount INTEGER NOT NULL CHECK (amount > 0),
    source_kind TEXT NOT NULL
        CHECK (source_kind IN ('income', 'general_remainder', 'accumulation')),
    source_income_id INTEGER REFERENCES incomes (id) ON DELETE RESTRICT,
    source_accumulation_id INTEGER REFERENCES accumulations (id) ON DELETE RESTRICT,
    CHECK (
        (source_kind = 'income' AND source_income_id IS NOT NULL AND source_accumulation_id IS NULL)
        OR (source_kind = 'general_remainder'
            AND source_income_id IS NULL AND source_accumulation_id IS NULL)
        OR (source_kind = 'accumulation'
            AND source_income_id IS NULL AND source_accumulation_id IS NOT NULL)
    )
) STRICT;
CREATE INDEX debt_repayments_debt ON debt_repayments (debt_id);
CREATE INDEX debt_repayments_month ON debt_repayments (month);

-- Базовий мінімум: значення для календарного місяця; не план і не ліміт (ADR 0002).
CREATE TABLE base_minimums (
    month TEXT PRIMARY KEY CHECK ({_month("month")}),
    amount INTEGER NOT NULL CHECK (amount >= 0)
) STRICT;
"""
