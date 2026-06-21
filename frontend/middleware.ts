import { NextRequest, NextResponse } from "next/server";

const PUBLIC_PATHS = [
  "/auth/login",
  "/auth/callback",
  "/auth/qr-redeem",
  "/get-app",
];

export function middleware(request: NextRequest) {
  const { pathname } = request.nextUrl;

  if (PUBLIC_PATHS.some((p) => pathname.startsWith(p))) {
    return NextResponse.next();
  }

  // Check cookie presence — actual JWT verification is done by the backend
  const hasAuthCookie = request.cookies.has("access_token");
  if (!hasAuthCookie) {
    const loginUrl = new URL("/auth/login", request.url);
    loginUrl.searchParams.set("next", pathname);
    return NextResponse.redirect(loginUrl);
  }

  return NextResponse.next();
}

export const config = {
  // Exclude static assets and the PDF.js worker (root-level public file) so the
  // viewer can load the worker without an auth redirect.
  matcher: [
    "/((?!_next/static|_next/image|favicon.ico|public/|api/|pdf.worker.min.mjs|manifest.json|sw.js).*)",
  ],
};
