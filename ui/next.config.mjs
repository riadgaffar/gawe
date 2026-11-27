/** @type {import('next').NextConfig} */
const nextConfig = {
  poweredByHeader: false,
  // `next dev` would otherwise write ui/AGENTS.md on every start.
  agentRules: false,
  async headers() {
    return [
      {
        source: "/(.*)",
        headers: [
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "X-Frame-Options", value: "DENY" },
          { key: "Referrer-Policy", value: "no-referrer" },
        ],
      },
    ];
  },
};
export default nextConfig;
