# Budget

> `Budget` — робоча назва проєкту. Остаточну назву продукту ще не затверджено.

Desktop-застосунок для Windows для керування персональним бюджетом. Він призначений для обліку отриманих доходів і фактичних витрат, перегляду доступних залишків і накопичень, а також аналізу фінансової ситуації за бюджетні періоди.

## Статус

Проєкт перебуває на **етапі архітектурної підготовки**. Код застосунку ще не написано, збірок і релізів немає.

Затверджено:

- технологічний стек;
- шари застосунку;
- принципи збереження даних;
- підхід до Windows-інсталятора;
- напрям дизайн-системи.

**Бізнес-модель і схема бази даних ще не зафіксовані.** Окремі частини моделі залежать від відкритих питань O-13, O-14, перелічених в архітектурній записці. Відкрите питання O-9 (модель боргу) блокує лише частину моделі та схеми бази, що стосується боргу. Правила бюджетного періоду (O-4) вирішено — див. [`docs/decisions/0002-budget-period.md`](docs/decisions/0002-budget-period.md). Механіку доходів і джерел витрат (O-2) вирішено — див. [`docs/decisions/0003-income-and-undistributed-funds.md`](docs/decisions/0003-income-and-undistributed-funds.md). Рахунки (O-3) вирішено: банківські рахунки в першому релізі не моделюються — див. [`docs/decisions/0004-accounts.md`](docs/decisions/0004-accounts.md). Поділ витрат на «базові / бажані» (O-5) у першому релізі не використовується — див. [`docs/decisions/0005-basic-desired-expense.md`](docs/decisions/0005-basic-desired-expense.md). Фінансові цілі (O-6) входять до першого релізу без окремої сутності: їхню функцію виконує накопичення — див. [`docs/decisions/0006-financial-goals.md`](docs/decisions/0006-financial-goals.md). Властивості та життєвий цикл накопичень (O-15) вирішено — див. [`docs/decisions/0007-accumulation-properties-lifecycle.md`](docs/decisions/0007-accumulation-properties-lifecycle.md). Валюту першого релізу (O-12) вирішено: усі грошові величини — лише в гривнях (UAH), без мультивалютності й конвертації — див. [`docs/decisions/0008-currency-first-release.md`](docs/decisions/0008-currency-first-release.md). Життєвий цикл доходу (O-11) вирішено: бюджетний період — календарний місяць без окремої сутності, дохід після створення не змінюється й не видаляється, а архівується автоматично — див. [`docs/decisions/0009-income-lifecycle-calendar-month.md`](docs/decisions/0009-income-lifecycle-calendar-month.md). Календарну належність фінансових операцій і стартові залишки первинного налаштування (Q151–Q167) зафіксовано — див. [`docs/decisions/0010-calendar-operations-initial-balances.md`](docs/decisions/0010-calendar-operations-initial-balances.md). Редагування операцій і межі первинного налаштування (Q168–Q173) зафіксовано, O-10 закрито як неактуальне — див. [`docs/decisions/0011-transaction-editing-initialization.md`](docs/decisions/0011-transaction-editing-initialization.md).

## Платформа

- Windows 10 і Windows 11, x64.

## Документація

- [`docs/architecture.md`](docs/architecture.md) — архітектурна записка: затверджені рішення, відкриті питання, цільова структура, межі шарів, сховище, дизайн-система, пакування та CI.
- [`docs/decisions/`](docs/decisions/) — журнал архітектурних рішень (ADR).
- [`docs/design/`](docs/design/) — матеріали дизайн-рев'ю (поки порожньо).
