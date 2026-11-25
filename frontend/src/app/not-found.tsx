import Link from "next/link";

export default function NotFound() {
  return (
    <div className="flex flex-col items-center gap-3 py-20 text-center">
      <p className="label">404</p>
      <h1 className="text-2xl font-semibold">Nothing here</h1>
      <p className="text-sm text-muted">
        That page does not exist, or the run was cleaned up by the demo reset.
      </p>
      <Link href="/" className="btn-primary mt-2">
        Back to the overview
      </Link>
    </div>
  );
}
