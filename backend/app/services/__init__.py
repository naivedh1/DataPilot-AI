"""Stateless capabilities the agent nodes compose.

Each service is independently unit-testable and knows nothing about LangGraph:
llm (Gemini client), schema_retrieval, sql_validator, analysis, visualization.
"""
