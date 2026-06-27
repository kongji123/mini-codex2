from __future__ import annotations

from abc import ABC, abstractmethod

from minicodex2.model.messages import ModelRequest, ModelResponse


class ModelAdapter(ABC):
    @abstractmethod
    def complete(self, request: ModelRequest) -> ModelResponse:
        raise NotImplementedError

