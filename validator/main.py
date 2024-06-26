import itertools
import numpy as np
from functools import partial
from typing import Any, Callable, Dict, List, Optional, Tuple
from collections import Counter
import time
import statistics

import numpy as np

from guardrails import Guard
from guardrails.llm_providers import PromptCallableException
from guardrails.utils.docs_utils import get_chunks_from_text
from guardrails.validator_base import (
    FailResult,
    PassResult,
    ValidationResult,
    Validator,
    register_validator,
)
from llama_index.embeddings.openai import OpenAIEmbedding
import openai


def _embed_function(text):
    if isinstance(text, str):
        text = [text]

    embeddings_out = []
    for current_example in text:
        embeddings_out.append(OpenAIEmbedding(model="text-embedding-ada-002").get_text_embedding(current_example))
    return np.array(embeddings_out)


@register_validator(name="arize/jailbreak_embeddings", data_type="string")
class JailbreakEmbeddings(Validator):
    """Validates that user-generated input does not match dataset of jailbreak
    embeddings from Arize AI."""

    def __init__(
            self,
            threshold: float = 0.8,
            validation_method: str = "full",
            on_fail: Optional[Callable] = None,
            **kwargs,
    ):
        super().__init__(
            on_fail, threshold=threshold, validation_method=validation_method, **kwargs
        )
        self._threshold = float(threshold)
        if validation_method not in ["full"]:
            raise ValueError("validation_method must be 'full'.")
        self._validation_method = "full"
        
        # users may send in their own source examples, but by default the module will load its own if not specified
        if kwargs.get("sources") is None:
            import os
            script_dir = os.path.dirname(__file__)  # Get the directory where the script is located
            file_path = os.path.join(script_dir, 'jailbreak_examples.txt')
            with open(file_path, 'r') as file:
                self.sources = file.read().splitlines()
            # few shot only plz
            self.sources = self.sources[:5]
        else:
            self.sources = kwargs.get("sources")


        self.embed_function = kwargs.get("embed_function", None)
        if self.embed_function is None:
            self.embed_function = _embed_function

        # Check chunking strategy
        chunk_strategy = kwargs.get("chunk_strategy", "sentence")
        if chunk_strategy not in ["sentence", "word", "char", "token"]:
            raise ValueError(
                "`chunk_strategy` must be one of 'sentence', 'word', 'char', "
                "or 'token'."
            )
        chunk_size = kwargs.get("chunk_size", 100)
        chunk_overlap = kwargs.get("chunk_overlap", 20)

        chunks = [
            get_chunks_from_text(source, chunk_strategy, chunk_size, chunk_overlap)
            for source in self.sources
        ]
        self.chunks = list(itertools.chain.from_iterable(chunks))

        # Create embeddings
        self.source_embeddings = np.array(self.embed_function(self.chunks)).squeeze()

    def get_query_function(self, metadata: Dict[str, Any]) -> Callable:
        """Get the query function from metadata.

        If `query_function` is provided, it will be used. Otherwise, `sources` and
        `embed_function` will be used to create a default query function.
        """
        query_fn = metadata.get("query_function", None)

        # Check that query_fn or sources are provided
        if query_fn is not None:
            return query_fn

        # Check distance metric
        distance_metric = metadata.get("distance_metric", "cosine")
        if distance_metric not in ["cosine", "euclidean"]:
            raise ValueError(
                "`distance_metric` must be one of 'cosine' or 'euclidean'."
            )

        # Check embed model
        embed_function = metadata.get("embed_function", None)
        return partial(
            self.query_vector_collection,
            distance_metric=distance_metric,
            embed_function=embed_function,
        )

    def validate_full_text(
            self, value: Any, query_function: Callable, metadata: Dict[str, Any]
    ) -> ValidationResult:
        """Validate the full text in the response."""
        # Replace LLM response with user input prompt
        print("THIS IS WHAT THE VALUE IS {}".format(value))
        most_similar_chunks = query_function(text=value, k=1)
        if most_similar_chunks is None:
            metadata["highest_similarity_score"] = 0
            metadata["similar_jailbreak_phrase"] = ""
            return PassResult(metadata=metadata)

        most_similar_chunk = most_similar_chunks[0]
        metadata["highest_similarity_score"] = most_similar_chunk[1]
        metadata["similar_jailbreak_phrase"] = most_similar_chunk[0]
        if most_similar_chunk[1] < self._threshold:
            return FailResult(
                metadata=metadata,
                error_message=(
                        "The following text in your response is similar to our dataset of jailbreaks prompts:\n" + value
                ),
            )
        return PassResult(metadata=metadata)

    def validate(self, value: Any, metadata: Dict[str, Any]) -> ValidationResult:
        """Validation function for the ProvenanceEmbeddings validator."""
        query_function = self.get_query_function(metadata)

        return self.validate_full_text(value, query_function, metadata)

    def query_vector_collection(
            self,
            text: str,
            k: int,
            embed_function: Callable,
            distance_metric: str = "cosine",
    ) -> List[Tuple[str, float]]:

        # Create embeddings
        print("THIS IS MY EMBED FUNC {}".format(self.embed_function))
        query_embedding = self.embed_function(text).squeeze()

        # Compute distances
        if distance_metric == "cosine":
            cos_sim = 1 - (
                    np.dot(self.source_embeddings, query_embedding)
                    / (
                            np.linalg.norm(self.source_embeddings, axis=1)
                            * np.linalg.norm(query_embedding)
                    )
            )
            top_indices = np.argsort(cos_sim)[:k]
            top_similarities = [cos_sim[j] for j in top_indices]
            top_chunks = [self.chunks[j] for j in top_indices]
        else:
            raise ValueError("distance_metric must be 'cosine'.")

        return list(zip(top_chunks, top_similarities))
