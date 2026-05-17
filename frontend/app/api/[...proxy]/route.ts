import { NextRequest, NextResponse } from "next/server";

export const dynamic = "force-dynamic";

const BACKEND = (process.env.BACKEND_URL ?? "http://localhost:8000").replace(
  /\/+$/,
  "",
);

async function handler(req: NextRequest): Promise<NextResponse> {
  const path = req.nextUrl.pathname;
  const search = req.nextUrl.search;
  const upstream = `${BACKEND}${path}${search}`;

  const headers = new Headers(req.headers);
  headers.delete("host");

  const hasBody = req.method !== "GET" && req.method !== "HEAD";

  try {
    const res = await fetch(upstream, {
      method: req.method,
      headers,
      body: hasBody ? req.body : undefined,
      // Node.js fetch requires duplex:"half" when request body is a stream
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      ...(hasBody ? { duplex: "half" } : {}),
      cache: "no-store",
    } as RequestInit);

    return new NextResponse(res.body, {
      status: res.status,
      headers: res.headers,
    });
  } catch (err) {
    return NextResponse.json(
      { error: "Backend unavailable", detail: String(err) },
      { status: 502 },
    );
  }
}

export {
  handler as DELETE,
  handler as GET,
  handler as HEAD,
  handler as OPTIONS,
  handler as PATCH,
  handler as POST,
  handler as PUT,
};
