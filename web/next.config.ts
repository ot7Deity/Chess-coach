import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  reactStrictMode: true,
  // The dashboard has no server-side secrets and reads the API from the browser,
  // so the "X-Powered-By: Next.js" banner is pure fingerprinting with no upside.
  poweredByHeader: false,
  // The floating dev-tools badge sits on top of the page in every development
  // screenshot; errors still surface in the terminal and the browser console.
  devIndicators: false,
  // The Docker image sets NEXT_OUTPUT=standalone: Next traces exactly the files
  // the server needs into .next/standalone, so the runtime image carries no
  // node_modules tree. Left off elsewhere so `npm start` keeps working locally.
  ...(process.env.NEXT_OUTPUT === "standalone" ? { output: "standalone" as const } : {}),
};

export default nextConfig;
