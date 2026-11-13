import { useEffect, useRef, useSyncExternalStore, type ReactNode } from "react";

const subscribe = (changed: () => void) => {
  window.addEventListener("resize", changed);
  return () => window.removeEventListener("resize", changed);
};
const narrowScreen = () => window.innerWidth <= 1180;

/** Keep the same editor mounted when the review switches between layouts. */
export function WorkReviewRegion({ open, onClose, children }: {
  open: boolean; onClose: () => void; children: ReactNode;
}) {
  const narrow = useSyncExternalStore(subscribe, narrowScreen);
  const panel = useRef<HTMLElement>(null);
  const close = useRef(onClose);
  useEffect(() => { close.current = onClose; }, [onClose]);
  useEffect(() => {
    const region = panel.current;
    if (!open || !narrow || !region) return;
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const previousOverflow = document.body.style.overflow;
    const background: Array<[HTMLElement, boolean]> = [];
    // Isolate siblings along the ancestor path, including the surrounding app.
    for (let child: HTMLElement = region; child.parentElement; child = child.parentElement) {
      for (const sibling of child.parentElement.children) {
        if (sibling instanceof HTMLElement && sibling !== child) {
          background.push([sibling, sibling.inert]); sibling.inert = true;
        }
      }
      if (child.parentElement === document.body) break;
    }
    document.body.style.overflow = "hidden";
    const controls = () => [...region.querySelectorAll<HTMLElement>(
      "button:not(:disabled), a[href], input:not(:disabled), select:not(:disabled), textarea:not(:disabled), summary, [tabindex]:not([tabindex='-1'])",
    )].filter((element) => {
      for (let parent: HTMLElement | null = element; parent && parent !== region; parent = parent.parentElement) {
        if (parent.hidden || getComputedStyle(parent).display === "none") return false;
        if (parent instanceof HTMLDetailsElement && !parent.open && parent.querySelector(":scope > summary") !== element) return false;
      }
      return true;
    });
    const focusFirst = () => (controls()[0] ?? region).focus();
    const nestedDialog = () => Boolean(document.querySelector("dialog[open]"));
    const keydown = (event: KeyboardEvent) => {
      if (nestedDialog()) return;
      if (event.key === "Escape") { event.preventDefault(); close.current(); }
      else if (event.key === "Tab") {
        const items = controls();
        const current = items.indexOf(document.activeElement as HTMLElement);
        if (!items.length || (event.shiftKey ? current <= 0 : current < 0 || current === items.length - 1)) {
          event.preventDefault(); (event.shiftKey ? items.at(-1) ?? region : items[0] ?? region).focus();
        }
      }
    };
    const focusin = (event: FocusEvent) => {
      if (!nestedDialog() && event.target instanceof Node && !region.contains(event.target)) focusFirst();
    };
    focusFirst();
    document.addEventListener("keydown", keydown);
    document.addEventListener("focusin", focusin);
    return () => {
      document.removeEventListener("keydown", keydown); document.removeEventListener("focusin", focusin);
      for (const [element, inert] of background) element.inert = inert;
      document.body.style.overflow = previousOverflow;
      if (previous?.isConnected) previous.focus();
    };
  }, [open, narrow]);
  return <aside ref={panel} className="work-plan-region" aria-label="当前方案审查" hidden={!open}
    role={narrow ? "dialog" : "complementary"} aria-modal={narrow && open ? true : undefined} tabIndex={-1}>
    {children}
  </aside>;
}
