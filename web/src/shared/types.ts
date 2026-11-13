/** Minimal handoff projection rendered by the Agent conversation feature. */
export type AgentHandoff = {
  from_agent?: string;
  to_agent?: string;
  reason?: string;
  required_outputs?: string[];
  status?: string;
  handoff_id?: string;
  blocking_items?: string[];
  data_references?: { input_selection_required?: boolean };
};
