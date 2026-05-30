import { redirect } from "next/navigation";

// llama.cpp management has been merged into the unified Модели section
// (Settings → Модели → Библиотека / Серверы). Keep this route as a redirect
// so old links and bookmarks still work.
export default function LlamacppRedirect() {
  redirect("/settings/models");
}
