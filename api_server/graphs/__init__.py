from .state import DesignState


def create_design_graph(*args, **kwargs):
    from .builder import create_design_graph as _create_design_graph

    return _create_design_graph(*args, **kwargs)


__all__ = ["create_design_graph", "DesignState"]
