import { useEffect, useState, type ReactNode } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  Loader2,
  LogIn,
  Mail,
  Sparkles,
  UserPlus,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { api, type AuthResult } from "@/lib/api";
import { useAuth } from "@/components/AuthProvider";

const INPUT =
  "h-10 w-full rounded-xl border border-border bg-card px-3 text-sm outline-none focus:ring-2 focus:ring-sky-500/40";

function Shell({ title, subtitle, children }: { title: string; subtitle?: string; children: ReactNode }) {
  return (
    <div className="mx-auto flex min-h-[60vh] max-w-md flex-col justify-center">
      <div className="mb-6 flex flex-col items-center gap-2 text-center">
        <div className="rounded-2xl bg-gradient-to-r from-sky-500 to-violet-500 p-2.5">
          <Sparkles className="h-6 w-6 text-white" />
        </div>
        <h1 className="text-2xl font-black tracking-tight">{title}</h1>
        {subtitle && <p className="text-sm text-muted-foreground">{subtitle}</p>}
      </div>
      <Card className="space-y-4 p-6">{children}</Card>
    </div>
  );
}

function Field({
  label,
  type = "text",
  value,
  onChange,
  placeholder,
  autoComplete,
}: {
  label: string;
  type?: string;
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  autoComplete?: string;
}) {
  return (
    <label className="block space-y-1">
      <span className="text-xs font-semibold text-muted-foreground">{label}</span>
      <input
        className={INPUT}
        type={type}
        value={value}
        autoComplete={autoComplete}
        placeholder={placeholder}
        spellCheck={false}
        onChange={(e) => onChange(e.target.value)}
      />
    </label>
  );
}

function ErrorMsg({ children }: { children: ReactNode }) {
  return (
    <div className="flex items-center gap-2 rounded-xl bg-rose-500/10 px-3 py-2 text-sm text-rose-500">
      <AlertTriangle className="h-4 w-4 shrink-0" /> {children}
    </div>
  );
}

function OkMsg({ children }: { children: ReactNode }) {
  return (
    <div className="flex items-center gap-2 rounded-xl bg-emerald-500/10 px-3 py-2 text-sm text-emerald-500">
      <CheckCircle2 className="h-4 w-4 shrink-0" /> {children}
    </div>
  );
}

function TextLink({ onClick, children }: { onClick: () => void; children: ReactNode }) {
  return (
    <button type="button" onClick={onClick} className="font-semibold text-sky-400 hover:underline">
      {children}
    </button>
  );
}

type Mode = "login" | "register" | "forgot";

function LoginForm({ onMode }: { onMode: (m: Mode) => void }) {
  const { setUser } = useAuth();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [needVerify, setNeedVerify] = useState(false);
  const [resent, setResent] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setErr(null);
    setNeedVerify(false);
    setResent(false);
    try {
      const r: AuthResult = await api.auth.login(email.trim(), password);
      if (r.ok && r.user) {
        setUser(r.user);
        return;
      }
      setNeedVerify(Boolean(r.needs_verification));
      setErr(r.error || "Não foi possível entrar.");
    } catch {
      setErr("Falha de conexão com o servidor.");
    } finally {
      setBusy(false);
    }
  };

  const resend = async () => {
    try {
      await api.auth.resendVerification(email.trim());
      setResent(true);
    } catch {
      /* generic — ignore */
    }
  };

  return (
    <form className="space-y-4" onSubmit={submit}>
      <Field label="E-mail" type="email" value={email} onChange={setEmail} autoComplete="email" placeholder="voce@exemplo.com" />
      <Field label="Senha" type="password" value={password} onChange={setPassword} autoComplete="current-password" />
      {err && <ErrorMsg>{err}</ErrorMsg>}
      {needVerify && !resent && (
        <button type="button" onClick={resend} className="text-sm font-semibold text-sky-400 hover:underline">
          Reenviar e-mail de confirmação
        </button>
      )}
      {resent && <OkMsg>Se a conta existir e estiver pendente, enviamos um novo link.</OkMsg>}
      <Button type="submit" disabled={busy} className="w-full">
        {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <LogIn className="h-4 w-4" />} Entrar
      </Button>
      <div className="flex items-center justify-between text-sm text-muted-foreground">
        <TextLink onClick={() => onMode("forgot")}>Esqueci minha senha</TextLink>
        <span>
          Não tem conta? <TextLink onClick={() => onMode("register")}>Cadastre-se</TextLink>
        </span>
      </div>
    </form>
  );
}

function RegisterForm({ onMode }: { onMode: (m: Mode) => void }) {
  const [fullName, setFullName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [done, setDone] = useState<string | null>(null);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setErr(null);
    if (!fullName.trim()) return setErr("Informe seu nome completo.");
    if (password !== confirm) return setErr("As senhas não coincidem.");
    if (password.length < 8) return setErr("A senha deve ter pelo menos 8 caracteres.");
    setBusy(true);
    try {
      const r: AuthResult = await api.auth.register(fullName.trim(), email.trim(), password, confirm);
      if (r.error) setErr(r.error);
      else setDone(r.message || "Verifique seu e-mail para ativar a conta.");
    } catch {
      setErr("Falha de conexão com o servidor.");
    } finally {
      setBusy(false);
    }
  };

  if (done) {
    return (
      <div className="space-y-4">
        <OkMsg>{done}</OkMsg>
        <p className="text-sm text-muted-foreground">
          Abra o link enviado para <strong>{email.trim()}</strong> para ativar a conta. Depois é só entrar.
        </p>
        <Button className="w-full" onClick={() => onMode("login")}>
          <LogIn className="h-4 w-4" /> Ir para o login
        </Button>
      </div>
    );
  }

  return (
    <form className="space-y-4" onSubmit={submit}>
      <Field label="Nome completo" value={fullName} onChange={setFullName} autoComplete="name" placeholder="Seu nome" />
      <Field label="E-mail" type="email" value={email} onChange={setEmail} autoComplete="email" placeholder="voce@exemplo.com" />
      <Field label="Senha" type="password" value={password} onChange={setPassword} autoComplete="new-password" placeholder="mín. 8 caracteres" />
      <Field label="Confirmar senha" type="password" value={confirm} onChange={setConfirm} autoComplete="new-password" />
      {err && <ErrorMsg>{err}</ErrorMsg>}
      <Button type="submit" disabled={busy} className="w-full">
        {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <UserPlus className="h-4 w-4" />} Criar conta
      </Button>
      <div className="text-center text-sm text-muted-foreground">
        Já tem conta? <TextLink onClick={() => onMode("login")}>Entrar</TextLink>
      </div>
    </form>
  );
}

function ForgotForm({ onMode }: { onMode: (m: Mode) => void }) {
  const [email, setEmail] = useState("");
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState<string | null>(null);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    try {
      const r = await api.auth.forgotPassword(email.trim());
      setDone(r.message || "Se esse e-mail estiver cadastrado, enviamos um link.");
    } catch {
      setDone("Se esse e-mail estiver cadastrado, enviamos um link.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className="space-y-4" onSubmit={submit}>
      {done ? (
        <OkMsg>{done}</OkMsg>
      ) : (
        <>
          <p className="text-sm text-muted-foreground">
            Informe seu e-mail e enviaremos um link para redefinir a senha.
          </p>
          <Field label="E-mail" type="email" value={email} onChange={setEmail} autoComplete="email" placeholder="voce@exemplo.com" />
          <Button type="submit" disabled={busy} className="w-full">
            {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <Mail className="h-4 w-4" />} Enviar link
          </Button>
        </>
      )}
      <div className="text-center text-sm text-muted-foreground">
        <TextLink onClick={() => onMode("login")}>Voltar ao login</TextLink>
      </div>
    </form>
  );
}

export function AuthForms() {
  const [mode, setMode] = useState<Mode>("login");
  const title = mode === "register" ? "Criar conta" : mode === "forgot" ? "Recuperar senha" : "Entrar";
  const subtitle =
    mode === "register"
      ? "Cadastre-se para acompanhar as entradas e resultados"
      : mode === "forgot"
      ? "Vamos te enviar um link por e-mail"
      : "Polymarket Sports";
  return (
    <Shell title={title} subtitle={subtitle}>
      {mode === "login" && <LoginForm onMode={setMode} />}
      {mode === "register" && <RegisterForm onMode={setMode} />}
      {mode === "forgot" && <ForgotForm onMode={setMode} />}
    </Shell>
  );
}

export function VerifyEmail({ token }: { token: string }) {
  const [state, setState] = useState<"loading" | "ok" | "fail">("loading");
  const [msg, setMsg] = useState("");

  useEffect(() => {
    let alive = true;
    (async () => {
      if (!token) {
        if (alive) {
          setState("fail");
          setMsg("Token ausente no link.");
        }
        return;
      }
      try {
        const r = await api.auth.verify(token);
        if (!alive) return;
        if (r.ok) setState("ok");
        else {
          setState("fail");
          setMsg(r.error || "Link inválido ou expirado.");
        }
      } catch {
        if (alive) {
          setState("fail");
          setMsg("Falha de conexão com o servidor.");
        }
      }
    })();
    return () => {
      alive = false;
    };
  }, [token]);

  return (
    <Shell title="Confirmação de e-mail">
      {state === "loading" && (
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin" /> Confirmando…
        </div>
      )}
      {state === "ok" && (
        <div className="space-y-4">
          <OkMsg>E-mail confirmado! Sua conta está ativa.</OkMsg>
          <Button className="w-full" onClick={() => window.location.assign("/")}>
            <LogIn className="h-4 w-4" /> Ir para o login
          </Button>
        </div>
      )}
      {state === "fail" && (
        <div className="space-y-4">
          <ErrorMsg>{msg}</ErrorMsg>
          <Button className="w-full" variant="outline" onClick={() => window.location.assign("/")}>
            Voltar ao início
          </Button>
        </div>
      )}
    </Shell>
  );
}

export function ResetPassword({ token }: { token: string }) {
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [done, setDone] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setErr(null);
    if (!token) return setErr("Token ausente no link.");
    if (password !== confirm) return setErr("As senhas não coincidem.");
    if (password.length < 8) return setErr("A senha deve ter pelo menos 8 caracteres.");
    setBusy(true);
    try {
      const r = await api.auth.resetPassword(token, password, confirm);
      if (r.ok) setDone(true);
      else setErr(r.error || "Link inválido ou expirado.");
    } catch {
      setErr("Falha de conexão com o servidor.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Shell title="Redefinir senha">
      {done ? (
        <div className="space-y-4">
          <OkMsg>Senha redefinida! Você já pode entrar com a nova senha.</OkMsg>
          <Button className="w-full" onClick={() => window.location.assign("/")}>
            <LogIn className="h-4 w-4" /> Ir para o login
          </Button>
        </div>
      ) : (
        <form className="space-y-4" onSubmit={submit}>
          <Field label="Nova senha" type="password" value={password} onChange={setPassword} autoComplete="new-password" placeholder="mín. 8 caracteres" />
          <Field label="Confirmar nova senha" type="password" value={confirm} onChange={setConfirm} autoComplete="new-password" />
          {err && <ErrorMsg>{err}</ErrorMsg>}
          <Button type="submit" disabled={busy} className="w-full">
            {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <CheckCircle2 className="h-4 w-4" />} Redefinir senha
          </Button>
        </form>
      )}
    </Shell>
  );
}
