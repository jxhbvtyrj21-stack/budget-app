from budget.domain.models import SetupStatus
from budget.storage.setup_repository import SetupStateRepository
from budget.storage.transaction import transaction


def test_draft_lifecycle(connection):
    repository = SetupStateRepository(connection)
    assert repository.get().status is SetupStatus.NOT_STARTED
    with transaction(connection):
        repository.save_draft({"initial_balance": "1000", "notes": "кирилиця"})
    state = repository.get()
    assert state.status is SetupStatus.IN_PROGRESS
    assert state.draft == {"initial_balance": "1000", "notes": "кирилиця"}
    with transaction(connection):
        repository.clear_draft()
    assert repository.get().status is SetupStatus.NOT_STARTED
    assert repository.get().draft is None
