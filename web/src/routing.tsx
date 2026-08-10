/* eslint-disable react-refresh/only-export-components -- tiny dependency-free router */
import { createContext, useContext, useEffect, useMemo, useState } from "react";

type RouterContextValue = {
  pathname: string;
  navigate: (to: string, replace?: boolean) => void;
};

const RouterContext = createContext<RouterContextValue | null>(null);

export function Router({ children }: { children: React.ReactNode }) {
  const [pathname, setPathname] = useState(window.location.pathname);
  useEffect(() => {
    const onPopState = () => setPathname(window.location.pathname);
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, []);
  const value = useMemo<RouterContextValue>(
    () => ({
      pathname,
      navigate: (to, replace = false) => {
        window.history[replace ? "replaceState" : "pushState"]({}, "", to);
        setPathname(window.location.pathname);
      },
    }),
    [pathname],
  );
  return <RouterContext.Provider value={value}>{children}</RouterContext.Provider>;
}

export function usePathname() {
  const context = useContext(RouterContext);
  if (!context) throw new Error("usePathname must be used inside Router");
  return context.pathname;
}

type LinkProps = {
  to: string;
  children: React.ReactNode;
  className?: string;
  "aria-current"?: "page";
};

export function Link({ to, children, className, ...rest }: LinkProps) {
  const context = useContext(RouterContext);
  if (!context) throw new Error("Link must be used inside Router");
  return (
    <a
      {...rest}
      className={className}
      href={to}
      onClick={(event) => {
        if (
          event.button === 0 &&
          !event.metaKey &&
          !event.ctrlKey &&
          !event.shiftKey &&
          !event.altKey
        ) {
          event.preventDefault();
          context.navigate(to);
        }
      }}
    >
      {children}
    </a>
  );
}

export function NavLink({ to, end, children }: LinkProps & { end?: boolean }) {
  const pathname = usePathname();
  const active = end ? pathname === to : pathname === to || pathname.startsWith(`${to}/`);
  return (
    <Link
      className={active ? "active" : undefined}
      to={to}
      aria-current={active ? "page" : undefined}
    >
      {children}
    </Link>
  );
}
