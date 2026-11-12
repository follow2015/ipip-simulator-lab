class SimulationError(ValueError):
    status = 400


class Conflict(SimulationError):
    status = 409


class NotFound(SimulationError):
    status = 404


class Unavailable(SimulationError):
    status = 503

    def __init__(self, message, outcome="not_executed", operation_id=None):
        super().__init__(message)
        self.outcome = outcome
        self.operation_id = operation_id

