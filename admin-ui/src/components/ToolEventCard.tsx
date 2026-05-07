import { useMemo, useState } from 'react';
import { ChevronDown, Clock3, Terminal, Wrench } from 'lucide-react';

export interface ToolEventViewModel {
  event_id: string;
  event_type: 'tool_event';
  run_id: string;
  timestamp: string;
  node_id: string;
  node_type: string;
  tool_name: string;
  status: 'success' | 'error';
  error_code: string;
  duration_ms: number;
  tool_input: Record<string, unknown>;
  tool_output: Record<string, unknown>;
}

interface ToolEventCardProps {
  event: ToolEventViewModel;
}

const MAX_PREVIEW_LENGTH = 220;
const MAX_COMMAND_LENGTH = 180;

function summarizeValue(value: Record<string, unknown>): string {
  const serialized = JSON.stringify(value, null, 2);
  return serialized.length <= MAX_PREVIEW_LENGTH
    ? serialized
    : `${serialized.slice(0, MAX_PREVIEW_LENGTH)}...`;
}

function summarizeCommand(event: ToolEventViewModel): string {
  const input = event.tool_input || {};
  const command = input.command ?? input.query ?? input.path ?? input.files ?? input.pattern;
  const rawSummary = command === undefined
    ? `${event.tool_name} ${JSON.stringify(input)}`
    : `${event.tool_name} ${Array.isArray(command) ? command.join(', ') : String(command)}`;
  const compactSummary = rawSummary.replace(/\s+/g, ' ').trim();
  return compactSummary.length <= MAX_COMMAND_LENGTH
    ? compactSummary
    : `${compactSummary.slice(0, MAX_COMMAND_LENGTH)}...`;
}

export function ToolEventCard({ event }: ToolEventCardProps) {
  const [expanded, setExpanded] = useState(false);
  const commandSummary = useMemo(() => summarizeCommand(event), [event]);
  const inputPreview = useMemo(() => summarizeValue(event.tool_input), [event.tool_input]);
  const outputPreview = useMemo(() => summarizeValue(event.tool_output), [event.tool_output]);
  const hasLongOutput = outputPreview.length >= MAX_PREVIEW_LENGTH;

  return (
    <div className="rounded-xl border border-sky-500/25 bg-slate-950/80 px-3 py-2 text-slate-100 shadow-[0_8px_24px_rgba(14,116,144,0.12)]">
      <div className="flex min-w-0 items-center justify-between gap-3">
        <div className="flex min-w-0 items-center gap-2">
          <span className="rounded-lg bg-sky-400/12 p-1.5 text-sky-300">
            <Wrench size={12} />
          </span>
          <div className="flex min-w-0 items-center gap-2">
            <span className="shrink-0 text-[10px] font-black uppercase tracking-[0.18em] text-sky-300">Tool Call</span>
            <span className={`shrink-0 rounded-full px-2 py-0.5 text-[9px] font-black uppercase tracking-wider ${event.status === 'success' ? 'bg-emerald-400/15 text-emerald-300' : 'bg-rose-400/15 text-rose-300'}`}>
              {event.status}
            </span>
            <span className="min-w-0 truncate font-mono text-[11px] leading-5 text-slate-200">
              {commandSummary}
            </span>
          </div>
        </div>

        <button
          type="button"
          onClick={() => setExpanded((prev) => !prev)}
          className="inline-flex shrink-0 items-center gap-1.5 rounded-lg border border-white/10 bg-white/5 px-2.5 py-1.5 text-[9px] font-black uppercase tracking-[0.18em] text-slate-200 transition-colors hover:bg-white/10"
        >
          {expanded ? 'Collapse' : 'Expand'}
          <ChevronDown size={11} className={`transition-transform ${expanded ? 'rotate-180' : ''}`} />
        </button>
      </div>

      {expanded && (
        <>
          <div className="mt-2 flex flex-wrap items-center gap-2 border-t border-white/10 pt-2 text-[10px] font-bold uppercase tracking-wider text-slate-300">
            <span className="rounded-full border border-white/10 bg-white/5 px-2.5 py-1">{event.node_type}</span>
            <span className="inline-flex items-center gap-1 rounded-full border border-white/10 bg-white/5 px-2.5 py-1">
              <Clock3 size={10} />
              {event.duration_ms} ms
            </span>
            <span className="rounded-full border border-white/10 bg-white/5 px-2.5 py-1">{event.error_code}</span>
          </div>

          <div className="mt-3 grid gap-3 lg:grid-cols-2">
            <section className="rounded-xl border border-white/8 bg-black/20 p-3">
              <div className="flex items-center gap-2 text-[10px] font-black uppercase tracking-[0.2em] text-slate-400">
                <Terminal size={11} />
                Params
              </div>
              <pre className="mt-2 whitespace-pre-wrap break-all font-mono text-[11px] leading-relaxed text-sky-100">{inputPreview}</pre>
            </section>
            <section className="rounded-xl border border-white/8 bg-black/20 p-3">
              <div className="flex items-center justify-between gap-3">
                <div className="text-[10px] font-black uppercase tracking-[0.2em] text-slate-400">Result</div>
                {hasLongOutput && (
                  <span className="text-[9px] font-black uppercase tracking-[0.2em] text-slate-500">
                    Full payload
                  </span>
                )}
              </div>
              <pre className="mt-2 whitespace-pre-wrap break-all font-mono text-[11px] leading-relaxed text-emerald-100">
                {expanded ? JSON.stringify(event.tool_output, null, 2) : outputPreview}
              </pre>
            </section>
          </div>
        </>
      )}
    </div>
  );
}
