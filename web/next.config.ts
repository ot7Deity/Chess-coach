import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  reactStrictMode: true,
  // The dashboard has no server-side secrets and reads the API from the browser,
  // so the "X-Powered-By: Next.js" banner is pure fingerprinting with no upside.
  poweredByHeader: false,
};

export default nextConfig;
