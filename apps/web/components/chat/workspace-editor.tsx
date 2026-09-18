"use client";
import CodeMirror from "@uiw/react-codemirror";
import { javascript } from "@codemirror/lang-javascript";
import { python } from "@codemirror/lang-python";
export default function WorkspaceEditor({ value, onChange, readOnly, path }: { value: string; onChange: (value: string) => void; readOnly: boolean; path: string }) {
  const extensions = path.endsWith(".py") ? [python()] : /\.[jt]sx?$/.test(path) ? [javascript({ jsx: true, typescript: /\.tsx?$/.test(path) })] : [];
  return <CodeMirror aria-label={`Contents of ${path}`} value={value} onChange={onChange} readOnly={readOnly} extensions={extensions} height="100%" minHeight="280px" className="min-h-0 flex-1 overflow-auto text-xs" basicSetup={{ foldGutter: true, lineNumbers: true, highlightActiveLine: !readOnly }} />;
}
