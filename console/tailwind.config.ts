import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./app/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        base: "#0E1116",
        raised: "#161B22",
        overlay: "#1C2430",
        ink: "#E6EDF3",
        muted: "#9AA7B4",
        accent: "#7C7CF0"
      }
    }
  },
  plugins: []
};

export default config;
