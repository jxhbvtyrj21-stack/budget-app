# Budget

> `Budget` — остаточна назва продукту (див. [`docs/decisions/0015-product-identity.md`](docs/decisions/0015-product-identity.md)).

Desktop-застосунок для Windows для керування персональним бюджетом. Він призначений для обліку отриманих доходів і фактичних витрат, перегляду доступних залишків і накопичень, а також аналізу фінансової ситуації за бюджетні періоди.

## Статус

Проєкт перебуває на **етапі архітектурної підготовки**. Код застосунку ще не написано, збірок і релізів немає.

Затверджено:

- технологічний стек;
- шари застосунку;
- принципи збереження даних;
- підхід до Windows-інсталятора;
- напрям дизайн-системи.

**Бізнес-модель першого релізу зафіксовано; схему бази даних ще не спроєктовано.** Відкритих O-питань в архітектурній записці немає. Правила бюджетного періоду (O-4) вирішено — див. [`docs/decisions/0002-budget-period.md`](docs/decisions/0002-budget-period.md). Механіку доходів і джерел витрат (O-2) вирішено — див. [`docs/decisions/0003-income-and-undistributed-funds.md`](docs/decisions/0003-income-and-undistributed-funds.md). Рахунки (O-3) вирішено: банківські рахунки в першому релізі не моделюються — див. [`docs/decisions/0004-accounts.md`](docs/decisions/0004-accounts.md). Поділ витрат на «базові / бажані» (O-5) у першому релізі не використовується — див. [`docs/decisions/0005-basic-desired-expense.md`](docs/decisions/0005-basic-desired-expense.md). Фінансові цілі (O-6) входять до першого релізу без окремої сутності: їхню функцію виконує накопичення — див. [`docs/decisions/0006-financial-goals.md`](docs/decisions/0006-financial-goals.md). Властивості та життєвий цикл накопичень (O-15) вирішено — див. [`docs/decisions/0007-accumulation-properties-lifecycle.md`](docs/decisions/0007-accumulation-properties-lifecycle.md). Валюту першого релізу (O-12) вирішено: усі грошові величини — лише в гривнях (UAH), без мультивалютності й конвертації — див. [`docs/decisions/0008-currency-first-release.md`](docs/decisions/0008-currency-first-release.md). Життєвий цикл доходу (O-11) вирішено: бюджетний період — календарний місяць без окремої сутності, дохід після створення не змінюється й не видаляється, а архівується автоматично — див. [`docs/decisions/0009-income-lifecycle-calendar-month.md`](docs/decisions/0009-income-lifecycle-calendar-month.md). Календарну належність фінансових операцій і стартові залишки первинного налаштування (Q151–Q167) зафіксовано — див. [`docs/decisions/0010-calendar-operations-initial-balances.md`](docs/decisions/0010-calendar-operations-initial-balances.md). Редагування операцій і межі первинного налаштування (Q168–Q173) зафіксовано, O-10 закрито як неактуальне — див. [`docs/decisions/0011-transaction-editing-initialization.md`](docs/decisions/0011-transaction-editing-initialization.md). Редагування поповнень, незавершене первинне налаштування й архівування накопичень (Q174–Q185) зафіксовано — див. [`docs/decisions/0012-accumulation-archive-initialization.md`](docs/decisions/0012-accumulation-archive-initialization.md). Межі архівування накопичень і межу первинного налаштування (Q186–Q190) зафіксовано — див. [`docs/decisions/0013-accumulation-archive-setup-boundaries.md`](docs/decisions/0013-accumulation-archive-setup-boundaries.md). Редагування метаданих операцій архівованого накопичення (Q191) зафіксовано — див. [`docs/decisions/0014-archived-transaction-metadata.md`](docs/decisions/0014-archived-transaction-metadata.md). Ідентичність продукту (O-1) — [`0015`](docs/decisions/0015-product-identity.md); екрани першого релізу (O-7) — [`0016`](docs/decisions/0016-first-release-screens.md); політика хвилин CI (O-8) — [`0017`](docs/decisions/0017-ci-minutes-policy.md); модель боргу (O-9) — [`0018`](docs/decisions/0018-debt-model.md); планування (O-14) — [`0019`](docs/decisions/0019-planning-scope.md); накопичення в аналізі періоду (O-13) — [`0020`](docs/decisions/0020-accumulations-in-period-analysis.md). Дизайн-рев'ю (R-7) завершено — див. [`docs/decisions/0021-design-review.md`](docs/decisions/0021-design-review.md). Чотири питання дизайн-рев'ю вирішено — див. [`docs/decisions/0022-design-review-decisions.md`](docs/decisions/0022-design-review-decisions.md).

## Платформа

- Windows 10 і Windows 11, x64.

## Документація

- [`docs/architecture.md`](docs/architecture.md) — архітектурна записка: затверджені рішення, відкриті питання, цільова структура, межі шарів, сховище, дизайн-система, пакування та CI.
- [`docs/decisions/`](docs/decisions/) — журнал архітектурних рішень (ADR).
- [`docs/design/design-system.md`](docs/design/design-system.md) — дизайн-система першого релізу.
- [`docs/design/ui-information-architecture.md`](docs/design/ui-information-architecture.md) — інформаційна архітектура інтерфейсу: екрани, навігація, форми, стани.
