---
title: Result Data & Objects
description: "Agently model response guide Result Data & Objects for unified text, structured data, metadata, and event streams."
keywords: "Agently,model response,streaming events,structured data,Result Data & Objects"
---

# Result Data & Objects

Agently keeps raw responses, parsed structured data, and metadata so you can consume results at different levels.

## get_text: final text

`get_text()` returns the final `done` text (even when output is JSON).

```python
text = response.result.get_text()
print(text)
```

## get_data: parsed / original / all

`get_data()` supports:

- `parsed`: structured result (default)
- `original`: raw provider response
- `all`: full result snapshot

```python
parsed = response.result.get_data(type="parsed")
original = response.result.get_data(type="original")
all_data = response.result.get_data(type="all")
```

Key fields inside `all`:

- `meta`: usage, finish_reason, etc.  
- `original_delta` / `original_done`: raw stream chunks and final payload  
- `text_result`: final text  
- `cleaned_result` / `parsed_result`: cleaned JSON and parsed data  
- `result_object`: Pydantic object (if available)  
- `errors` / `extra`: errors and extra fields  

## get_data_object: typed output

When you use Agently Output Format (JSON), you can get a typed object:

```python
result_obj = response.result.get_data_object()
print(result_obj)
```

## ensure_keys: field guarantees

`get_data()` supports retries with `ensure_keys`:

```python
data = response.result.get_data(
  type="parsed",
  ensure_keys=["intro"],
  key_style="dot",
  max_retries=3,
  raise_ensure_failure=True,
)
```

`key_style` supports `dot` or `slash` paths.

## validate: business-rule guarantees

`get_data()` and `get_data_object()` also support custom validation:

```python
data = response.result.get_data(
  ensure_keys=["intro"],
  validate_handler=lambda result, context: result["intro"].strip() != "",
  max_retries=2,
)
```

Rules:

- `ensure_keys` checks path existence
- `validate` checks value correctness
- both share the same retry budget
- on one `response.result`, validate runs once and its outcome is reused across later `get_data()` / `get_data_object()` reads
