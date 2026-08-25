import { useEffect, useState } from "react";

export function navigate(path: string) {
  window.history.pushState(null, "", path);
  window.dispatchEvent(new PopStateEvent("popstate"));
}

export function useRoute(): string {
  const [route, setRoute] = useState(() => window.location.pathname);
  useEffect(() => {
    const update = () => setRoute(window.location.pathname);
    window.addEventListener("popstate", update);
    return () => window.removeEventListener("popstate", update);
  }, []);
  return route;
}

export function Link({
  to,
  onClick,
  ...rest
}: { to: string } & Omit<React.AnchorHTMLAttributes<HTMLAnchorElement>, "href">) {
  return (
    <a
      href={to}
      onClick={(event) => {
        onClick?.(event);
        // Modified clicks (new tab, download…) keep native behavior.
        if (
          event.defaultPrevented ||
          event.button !== 0 ||
          event.metaKey ||
          event.ctrlKey ||
          event.shiftKey ||
          event.altKey
        ) {
          return;
        }
        event.preventDefault();
        navigate(to);
      }}
      {...rest}
    />
  );
}
