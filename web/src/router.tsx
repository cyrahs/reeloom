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
  children,
}: {
  to: string;
  children: ReactNode;
}) {
  const path = useHashPath();
  return (
    <HashLink
      to={to}
      className={
        path === to || (to !== "/" && path.startsWith(`${to}/`))
          ? "active"
          : undefined
      }
    >
      {children}
    </HashLink>
  );
}
