import type { Metadata } from "next";
import { Dashboard } from "./Dashboard";

export const metadata: Metadata = {
  title: "AIS Collection Health",
  description:
    "Lightweight AIS collection volume, storage and anomaly monitoring.",
};

export default function Home() {
  return <Dashboard />;
}
