/** @type {import('next').NextConfig} */
const API_BASE = process.env.BP_API_BASE || "http://localhost:8000";

const nextConfig = {
  cacheComponents: true,
  reactStrictMode: true,
  experimental: {
    proxyTimeout: 600_000,
  },
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: `${API_BASE}/api/:path*`,
      },
    ];
  },
};

export default nextConfig;
