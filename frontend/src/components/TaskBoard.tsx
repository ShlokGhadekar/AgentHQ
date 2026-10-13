import type { TaskView } from "../lib/reduce";
import type { TaskStatus } from "../lib/types";

// Failed tasks sit in "To do": they wait there, marked in red pencil, for the Manager to retry them.
const COLUMNS: { title: string; statuses: TaskStatus[] }[] = [
  { title: "To do", statuses: ["todo", "failed"] },
  { title: "In progress", statuses: ["in_progress"] },
  { title: "Review", statuses: ["review"] },
  { title: "Done", statuses: ["done"] },
];

export function TaskBoard({ tasks }: { tasks: TaskView[] }) {
  return (
    <section aria-label="Task board" className="grid h-full min-h-0 grid-cols-4 divide-x divide-ink/15 border-t border-ink/15 bg-paper">
      {COLUMNS.map((column) => {
        const items = tasks.filter((t) => column.statuses.includes(t.status));
        return (
          <div key={column.title} className="flex min-h-0 flex-col">
            <h2 className="flex items-baseline justify-between px-3 pt-2 pb-1.5 text-base font-semibold">
              {column.title}
              <span className="text-sm font-normal text-ink/50 tabular-nums">{items.length}</span>
            </h2>
            <ul className="min-h-0 flex-1 space-y-1.5 overflow-y-auto px-3 pb-3">
              {items.map((task) => (
                <TaskCard key={task.id} task={task} />
              ))}
            </ul>
          </div>
        );
      })}
    </section>
  );
}

function TaskCard({ task }: { task: TaskView }) {
  const failed = task.status === "failed";
  return (
    <li
      className={`border bg-white px-2.5 py-1.5 text-sm ${failed ? "border-pencil" : "border-ink/20"}`}
      title={task.result ?? task.description}
    >
      <div className="flex items-baseline gap-2">
        <span className="font-semibold tabular-nums">{task.id}</span>
        <span className="min-w-0 flex-1 truncate">{task.title}</span>
      </div>
      <div className="flex justify-between text-xs text-ink/60">
        <span className="capitalize">{task.assignee}</span>
        <span>
          {failed && <span className="text-pencil">Failed. </span>}
          {task.attempts > 1 && `Attempt ${task.attempts}`}
          {task.dependsOn.length > 0 && task.status === "todo" && !failed && `After ${task.dependsOn.join(", ")}`}
        </span>
      </div>
    </li>
  );
}
