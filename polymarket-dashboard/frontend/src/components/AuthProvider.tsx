import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import { api, setUnauthorizedHandler, type User } from "@/lib/api";

interface AuthContextValue {
  user: User | null;
  loading: boolean;
  setUser: (u: User | null) => void;
  refresh: () => Promise<void>;
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    try {
      const r = await api.me();
      setUser(r.user);
    } catch {
      setUser(null); // not logged in (or session expired)
    }
  }, []);

  useEffect(() => {
    // Any 401 anywhere flips us back to the login screen immediately.
    setUnauthorizedHandler(() => setUser(null));
    refresh().finally(() => setLoading(false));
    return () => setUnauthorizedHandler(null);
  }, [refresh]);

  const logout = useCallback(async () => {
    try {
      await api.auth.logout();
    } catch {
      /* ignore — clear locally regardless */
    }
    setUser(null);
  }, []);

  return (
    <AuthContext.Provider value={{ user, loading, setUser, refresh, logout }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth deve ser usado dentro de <AuthProvider>");
  return ctx;
}
