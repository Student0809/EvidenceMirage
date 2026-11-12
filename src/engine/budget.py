from dataclasses import dataclass


@dataclass
class Budget:
    """Counts started Target executions, including errors; H1 is separate."""
    limit: int
    spent: int = 0

    def __post_init__(self):
        if type(self.limit) is not int or self.limit < 0:
            raise ValueError('Target budget must be a nonnegative integer')

    @property
    def remaining(self):
        return self.limit - self.spent

    def reserve(self):
        if not self.remaining:
            raise RuntimeError('Target budget exhausted')
        self.spent += 1
