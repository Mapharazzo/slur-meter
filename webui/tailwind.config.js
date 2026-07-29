/** @type {import('tailwindcss').Config} */
// Tailwind supplies layout utilities only. Every colour, surface and type
// decision lives in the design system in src/index.css, so the tokens below
// point back at the same custom properties rather than forking a palette.
export default {
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        stock: "var(--stock)",
        bay: "var(--bay)",
        rail: "var(--rail)",
        edge: "var(--edge)",
        print: "var(--print)",
        grey: "var(--grey)",
        tungsten: "var(--tungsten)",
        daylight: "var(--daylight)",
        sun: "var(--sun)",
        tally: "var(--tally)",
      },
      fontFamily: {
        mono: ["Martian Mono", "ui-monospace", "SF Mono", "Menlo", "monospace"],
        sans: ["Instrument Sans", "ui-sans-serif", "system-ui", "sans-serif"],
      },
    },
  },
  plugins: [],
}
