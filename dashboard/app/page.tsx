import type { Metadata } from "next";
import { Dashboard } from "./Dashboard";

export const metadata: Metadata = {
  title: "Netherlands AIS Pulse",
  description:
    "National AIS minute, hourly and operating-day collection monitoring.",
};

export default function Home() {
  return <Dashboard />;
}
