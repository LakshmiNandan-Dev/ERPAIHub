"""TinyLLM -- from-scratch NL->Oracle-SQL for Oracle E-Business Suite.

This package is the data engine: it turns schemas (synthetic for the vendor
base; extracted for customer-local specialization) into validated
(schema, question, SQL) training triples.
"""

from .pipeline import Example, example_from_schema, generate_example, serialize_schema

__all__ = ["Example", "generate_example", "example_from_schema", "serialize_schema"]

__version__ = "0.0.1"
