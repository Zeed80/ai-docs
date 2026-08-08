export type DigitizationType =
  | "auto"
  | "rotation_body"
  | "arbitrary_mechanical_part"
  | "mechanical_assembly"
  | "construction_structure"
  | "architectural_drawing"
  | "mep_systems"
  | "electrical_scheme"
  | "hydraulic_scheme"
  | "pid_scheme";

export const DIGITIZATION_TYPE_OPTIONS: {
  value: DigitizationType;
  labelKey: string;
}[] = [
  { value: "auto", labelKey: "type_auto" },
  { value: "rotation_body", labelKey: "type_rotation_body" },
  {
    value: "arbitrary_mechanical_part",
    labelKey: "type_arbitrary_mechanical_part",
  },
  { value: "mechanical_assembly", labelKey: "type_mechanical_assembly" },
  {
    value: "construction_structure",
    labelKey: "type_construction_structure",
  },
  { value: "architectural_drawing", labelKey: "type_architectural_drawing" },
  { value: "mep_systems", labelKey: "type_mep_systems" },
  { value: "electrical_scheme", labelKey: "type_electrical_scheme" },
  { value: "hydraulic_scheme", labelKey: "type_hydraulic_scheme" },
  { value: "pid_scheme", labelKey: "type_pid_scheme" },
];

export function profileForDigitizationType(type: DigitizationType): string {
  if (
    type === "rotation_body" ||
    type === "arbitrary_mechanical_part" ||
    type === "mechanical_assembly"
  ) {
    return "mechanical_eskd";
  }
  if (
    type === "construction_structure" ||
    type === "architectural_drawing" ||
    type === "mep_systems"
  ) {
    return "construction";
  }
  if (type === "electrical_scheme") return "electrical";
  if (type === "hydraulic_scheme") return "hydraulic";
  if (type === "pid_scheme") return "pid";
  return "auto";
}
