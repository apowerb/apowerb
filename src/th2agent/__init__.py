"""
Docstring for th2agent

"""

# Pydantic hack for Google ADK - must be applied before any google.adk imports
try:
    import mcp.client.session
    from pydantic_core import core_schema

    def __get_pydantic_core_schema__(cls, source_type, handler):
        return core_schema.is_instance_schema(cls)

    mcp.client.session.ClientSession.__get_pydantic_core_schema__ = classmethod(
        __get_pydantic_core_schema__
    )
except ImportError:
    pass
