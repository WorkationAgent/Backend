from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph

from app.core.state import GraphState
from app.graph.nodes import human_select_node, planner_finish_node, planner_start_node

_builder = StateGraph(GraphState)

_builder.add_node("planner_start",  planner_start_node)
_builder.add_node("human_select",   human_select_node)
_builder.add_node("planner_finish", planner_finish_node)

_builder.set_entry_point("planner_start")
_builder.add_edge("planner_start",  "human_select")
_builder.add_edge("human_select",   "planner_finish")
_builder.add_edge("planner_finish", END)

memory = MemorySaver()
graph = _builder.compile(checkpointer=memory)
