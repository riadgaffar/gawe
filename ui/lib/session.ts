// Server-only: the API token lives in an httpOnly cookie and is read here, never in the browser.
import { cookies } from "next/headers";
import { redirect } from "next/navigation";
import { ApiError, createClient, type Client } from "./api-client";
import type { MeView } from "./api-types";

export const TOKEN_COOKIE = "gawe_token";
const SESSION_SECONDS = 8 * 60 * 60;

export function apiBase(): string {
  return process.env.GAWE_API_URL ?? "http://127.0.0.1:8000";
}

export async function getToken(): Promise<string | null> {
  return (await cookies()).get(TOKEN_COOKIE)?.value ?? null;
}

export async function setToken(token: string): Promise<void> {
  (await cookies()).set(TOKEN_COOKIE, token, {
    httpOnly: true,
    sameSite: "lax",
    path: "/",
    secure: process.env.GAWE_UI_SECURE_COOKIE === "1", // set to 1 when served over HTTPS
    maxAge: SESSION_SECONDS,
  });
}

export async function clearToken(): Promise<void> {
  (await cookies()).delete(TOKEN_COOKIE);
}

export function clientFor(token: string): Client {
  return createClient({ baseUrl: apiBase(), token });
}

/** For pages and actions: the signed-in client and user, or a redirect to /login. */
export async function requireSession(): Promise<{ client: Client; me: MeView }> {
  const token = await getToken();
  if (!token) redirect("/login");
  const client = clientFor(token);
  try {
    return { client, me: await client.me() };
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) redirect("/login");
    throw error;
  }
}
