/** @type {import('tailwindcss').Config} */
export default {
  darkMode: "class",
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        border: "hsl(var(--border))",
        background: "hsl(var(--background))",
        foreground: "hsl(var(--foreground))",
        card: { DEFAULT: "hsl(var(--card))", foreground: "hsl(var(--card-foreground))" },
        muted: { DEFAULT: "hsl(var(--muted))", foreground: "hsl(var(--muted-foreground))" },
        // Domain accents
        over: "hsl(199 89% 48%)",
        under: "hsl(258 90% 66%)",
        win: "hsl(152 69% 45%)",
        loss: "hsl(351 83% 61%)",
        pending: "hsl(38 92% 55%)",
        voidc: "hsl(215 16% 55%)",
      },
      borderRadius: { xl: "1rem", "2xl": "1.25rem" },
      keyframes: {
        shimmer: { "100%": { transform: "translateX(100%)" } },
      },
    },
  },
  plugins: [],
};
