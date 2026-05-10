export function ComposerBar({
  value,
  onChange,
  onSend,
  disabled,
}: {
  value: string;
  onChange: (v: string) => void;
  onSend: () => void;
  disabled: boolean;
}) {
  return (
    <footer className="flex items-end gap-2 border-t border-[#222] p-3">
      <textarea
        className="min-h-[44px] flex-1 resize-y rounded-md border border-[#333] bg-[#1a1c20] px-2 py-2 font-inherit text-sm text-[#e6e6e6] outline-none focus:ring-1 focus:ring-blue-500"
        rows={2}
        value={value}
        placeholder="What do you do?"
        aria-label="Your action"
        disabled={disabled}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            onSend();
          }
        }}
      />
      <button
        type="button"
        className="rounded-md bg-[#3b82f6] px-4 py-2 font-semibold text-white disabled:cursor-not-allowed disabled:opacity-50"
        disabled={disabled}
        onClick={onSend}
      >
        Send
      </button>
    </footer>
  );
}
