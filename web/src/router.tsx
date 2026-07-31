import {
  type AnchorHTMLAttributes,
  type ReactNode,
  useEffect,
  useState,
} from "react";

export function useHashPath(): string {
  const [path, setPath] = useState(readPath);
  useEffect(() => {
    const update = () => setPath(readPath());
    window.addEventListener("hashchange", update);
    return () => window.removeEventListener("hashchange", update);
  }, []);
  return path;
}

function readPath(): string {
  const value = window.location.hash.slice(1);
  return value.startsWith("/") ? value : "/";
}

export function HashLink({
  to,
  children,
  ...props
}: {
  to: string;
  children: ReactNode;
} & Omit<AnchorHTMLAttributes<HTMLAnchorElement>, "href">) {
  return (
    <a href={`#${to}`} {...props}>
      {children}
    </a>
  );
}

export function HashNavLink({
  to,
  owns = [],
  children,
}: {
  to: string;
  /** Extra path prefixes this nav entry represents, e.g. "/runs/" under "/". */
  owns?: string[];
  children: ReactNode;
}) {
  const path = useHashPath();
  const active =
    path === to ||
    (to !== "/" && path.startsWith(`${to}/`)) ||
    owns.some((prefix) => path.startsWith(prefix));
  return (
    <HashLink to={to} className={active ? "active" : undefined}>
      {children}
    </HashLink>
  );
}
