# -*- coding: utf-8 -*-

"""Basic structure for a negative sampler."""

from abc import ABC, abstractmethod

import numpy as np
import torch

from ..instance_creation_factories import TriplesFactory

__all__ = [
    'NegativeSampler',
]


class NegativeSampler(ABC):
    """A negative sampler."""

    def __init__(self, triples_factory: TriplesFactory) -> None:
        """Initialize the negative sampler with the given entities."""
        self.triples_factory = triples_factory

    @property
    def num_entities(self) -> int:  # noqa: D401
        """The number of entities to sample from."""
        return self.triples_factory.num_entities

    @property
    def all_entities(self) -> np.ndarray:  # noqa: D401
        """The array of all entities."""
        return self.triples_factory.all_entities

    @abstractmethod
    def sample(self, positive_batch: torch.LongTensor) -> torch.LongTensor:
        """Generate negative samples from the positive batch."""
        raise NotImplementedError
