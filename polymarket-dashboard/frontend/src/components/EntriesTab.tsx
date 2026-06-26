import { useQuery } from "@tanstack/react-query";
import { Inbox } from "lucide-react";
import { Card } from "@/components/ui/card";
import { EntryCard } from "@/components/EntryCard";
import { api } from "@/lib/api";

export function EntriesTab() {
  const { data, isLoading } = useQuery({
    queryKey: ["entries"],
    queryFn: () => api.entries(),
    refetchInterval: 30 * 1000, // pick up new pushes
    refetchOnWindowFocus: false,
  });

  if (isLoading) {
    return (
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {Array.from({ length: 6 }).map((_, i) => (
          <div key={i} className="skeleton h-40" />
        ))}
      </div>
    );
  }

  const cats = data?.categories ?? [];
  if (cats.length === 0) {
    return (
      <Card className="flex flex-col items-center gap-2 p-10 text-center text-sm text-muted-foreground">
        <Inbox className="h-7 w-7" />
        Nenhuma entrada aberta no momento.
      </Card>
    );
  }

  return (
    <div className="space-y-7">
      {cats.map((c) => (
        <section key={c.category}>
          <div className="mb-2.5 flex items-center gap-2">
            <h2 className="text-sm font-black uppercase tracking-wider">{c.category}</h2>
            <span className="rounded-full bg-gradient-to-r from-sky-500/20 to-violet-500/20 px-2 py-0.5 text-xs font-bold">
              {c.entries.length}
            </span>
          </div>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {c.entries.map((e) => (
              <EntryCard key={e.key} e={e} />
            ))}
          </div>
        </section>
      ))}
    </div>
  );
}
