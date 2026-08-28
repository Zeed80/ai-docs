"use client";

import { useEditor, EditorContent } from "@tiptap/react";
import StarterKit from "@tiptap/starter-kit";
import Link from "@tiptap/extension-link";
import { useEffect } from "react";

/** HTML rich-text editor for the composer (TipTap). */
export function RichTextEditor({
  value,
  onChange,
  placeholder,
}: {
  value: string;
  onChange: (html: string) => void;
  placeholder?: string;
}) {
  const editor = useEditor({
    extensions: [
      StarterKit.configure({ heading: { levels: [2, 3] } }),
      Link.configure({ openOnClick: false, autolink: true }),
    ],
    content: value,
    immediatelyRender: false,
    editorProps: {
      attributes: {
        class:
          "min-h-[180px] max-h-[45vh] overflow-auto px-3 py-2 text-sm text-slate-100 focus:outline-none prose prose-invert prose-sm max-w-none",
        "data-placeholder": placeholder ?? "",
      },
    },
    onUpdate: ({ editor }) => onChange(editor.getHTML()),
  });

  useEffect(() => {
    if (editor && value !== editor.getHTML()) {
      editor.commands.setContent(value, false);
    }
  }, [value, editor]);

  if (!editor) {
    return <div className="min-h-[220px] rounded-lg border border-slate-600 bg-slate-900" />;
  }

  const btn = (active: boolean) =>
    `px-2 py-1 text-xs rounded font-medium ${
      active ? "bg-slate-600 text-white" : "text-slate-300 hover:bg-slate-700"
    }`;

  return (
    <div className="rounded-lg border border-slate-600 bg-slate-900 overflow-hidden">
      <div className="flex flex-wrap items-center gap-0.5 border-b border-slate-700 bg-slate-800 px-1 py-1">
        <button type="button" className={btn(editor.isActive("bold"))} onClick={() => editor.chain().focus().toggleBold().run()}>
          <b>B</b>
        </button>
        <button type="button" className={`${btn(editor.isActive("italic"))} italic`} onClick={() => editor.chain().focus().toggleItalic().run()}>
          I
        </button>
        <button type="button" className={`${btn(editor.isActive("strike"))} line-through`} onClick={() => editor.chain().focus().toggleStrike().run()}>
          S
        </button>
        <span className="mx-1 h-4 w-px bg-slate-700" />
        <button type="button" className={btn(editor.isActive("bulletList"))} onClick={() => editor.chain().focus().toggleBulletList().run()}>
          • List
        </button>
        <button type="button" className={btn(editor.isActive("orderedList"))} onClick={() => editor.chain().focus().toggleOrderedList().run()}>
          1. List
        </button>
        <button type="button" className={btn(editor.isActive("blockquote"))} onClick={() => editor.chain().focus().toggleBlockquote().run()}>
          ❝
        </button>
        <button
          type="button"
          className={btn(editor.isActive("link"))}
          onClick={() => {
            const prev = editor.getAttributes("link").href as string | undefined;
            const url = window.prompt("URL", prev ?? "https://");
            if (url === null) return;
            if (url === "") editor.chain().focus().unsetLink().run();
            else editor.chain().focus().extendMarkRange("link").setLink({ href: url }).run();
          }}
        >
          🔗
        </button>
      </div>
      <EditorContent editor={editor} />
    </div>
  );
}
