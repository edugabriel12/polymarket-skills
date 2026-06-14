import { motion } from "framer-motion";
import { cn } from "@/lib/utils";

interface Props {
  label: string;
  value: string;
  sub?: string;
  gradient: string; // tailwind gradient classes
  icon: React.ElementType;
  positive?: boolean | null;
}

export function KpiCard({ label, value, sub, gradient, icon: Icon, positive }: Props) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 12 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.3 }}
      className={cn(
        "relative overflow-hidden rounded-2xl p-5 text-white shadow-xl",
        "bg-gradient-to-br",
        gradient
      )}
    >
      <div className="absolute -right-6 -top-6 opacity-20">
        <Icon className="h-24 w-24" />
      </div>
      <div className="text-xs font-semibold uppercase tracking-wider text-white/80">{label}</div>
      <div className="mt-2 text-3xl font-black tabular-nums">{value}</div>
      {sub && (
        <div
          className={cn(
            "mt-1 text-xs font-medium",
            positive === true && "text-emerald-100",
            positive === false && "text-rose-100",
            (positive === null || positive === undefined) && "text-white/70"
          )}
        >
          {sub}
        </div>
      )}
    </motion.div>
  );
}
