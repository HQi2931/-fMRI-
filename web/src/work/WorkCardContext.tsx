/* eslint-disable react-refresh/only-export-components */
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type Dispatch, type ReactNode, type SetStateAction } from "react";
import { api, describeError, type WorkCard, type WorkCardOperation, type WorkCardResult } from "../api/client";

const mutations = new Set<WorkCardOperation>([
  "createProject", "createDataset", "inspectDataset", "importDemographics", "createSplit",
  "resolveSkillPlan", "approvePlan", "createRun", "cancelRun", "retryRun", "diagnoseRun",
  "createQcReview", "approveQcReview", "createStatisticalDesign", "validateStatisticalDesign",
  "createStatisticsRun", "inspectMlTable", "createMlTemplate", "validateRoiTable",
  "localizeClusters", "answerRsFmriQuestion", "organizationPreview",
]);
type CardContext = {
  draft: Record<string, unknown>;
  change: (key: string, value: unknown) => void;
  execute: (operation: WorkCardOperation, args: unknown[]) => Promise<unknown>;
};
const Context = createContext<CardContext | null>(null);

export function WorkCardProvider({ card, conversationId, onUpdate, children }: {
  card: WorkCard; conversationId: string; onUpdate: (result: WorkCardResult) => void; children: ReactNode;
}) {
  const current = useRef(card);
  const draft = useRef({ ...card.draft });
  const changed = useRef(false);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const queue = useRef<Promise<unknown>>(Promise.resolve());
  const callback = useRef(onUpdate);
  callback.current = onUpdate;
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);

  const execute = useCallback((operation: WorkCardOperation, args: unknown[]) => {
    const run = async () => {
      if (operation !== "saveDraft" && !current.current.allowed_operations.includes(operation)) {
        throw new Error("该操作不属于当前卡片，请在对话中打开对应功能。");
      }
      setSaving(true);
      try {
        const result = await api.workCardAction(conversationId, current.current.card_id, {
          expected_version: current.current.version, operation, args,
        });
        current.current = result.card;
        callback.current(result);
        setError("");
        return result.result;
      } catch (caught) {
        setError(`${describeError(caught)}；草稿仍保留，请刷新卡片后重试。`);
        throw caught;
      } finally { setSaving(false); }
    };
    const result = queue.current.then(run, run);
    queue.current = result.catch(() => undefined);
    return result;
  }, [conversationId]);

  const flush = useCallback(() => {
    clearTimeout(timer.current);
    if (!changed.current) return;
    changed.current = false;
    void execute("saveDraft", [{ ...draft.current }]).catch(() => { changed.current = true; });
  }, [execute]);

  useEffect(() => () => { flush(); }, [flush]);
  const value = useMemo<CardContext>(() => ({
    draft: draft.current,
    change: (key, next) => {
      draft.current[key] = next;
      changed.current = true;
      clearTimeout(timer.current);
      timer.current = setTimeout(flush, 500);
    },
    execute: (operation, args) => { flush(); return execute(operation, args); },
  }), [execute, flush]);
  return <Context.Provider value={value}>
    <div className="work-card-save-state" role="status">{error || (saving ? "正在保存…" : "填写内容自动保存到当前对话")}</div>
    {children}
  </Context.Provider>;
}

export function useCardState<T>(key: string, initial: T | (() => T)): [T, Dispatch<SetStateAction<T>>] {
  const context = useContext(Context);
  const [value, setValue] = useState<T>(() => context && key in context.draft
    ? context.draft[key] as T : typeof initial === "function" ? (initial as () => T)() : initial);
  const valueRef = useRef(value);
  const set: Dispatch<SetStateAction<T>> = useCallback((next) => {
    const resolved = typeof next === "function" ? (next as (value: T) => T)(valueRef.current) : next;
    valueRef.current = resolved;
    setValue(resolved);
    context?.change(key, resolved);
  }, [context, key]);
  return [value, set];
}

export function useWorkCard() { return useContext(Context); }

export function useBusinessApi(): typeof api {
  const context = useContext(Context);
  return useMemo(() => context ? new Proxy(api, {
    get(target, key: keyof typeof api) {
      if (!mutations.has(key as WorkCardOperation)) return target[key];
      return (...args: unknown[]) => context.execute(key as WorkCardOperation, args.filter((arg) =>
        arg !== undefined && !(typeof AbortSignal !== "undefined" && arg instanceof AbortSignal),
      ));
    },
  }) : api, [context]);
}
