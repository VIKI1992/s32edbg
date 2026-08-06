import * as vscode from "vscode";
import { setting } from "../config";

/** Modal confirmation for actions that reset the SoC or write target state.
 * Returns true when the user (or the s32e.confirmDestructive=false setting)
 * says go.
 */
export async function confirmDestructive(
  title: string,
  detail: string,
  goLabel = "Proceed",
): Promise<boolean> {
  if (!setting<boolean>("confirmDestructive", true)) {
    return true;
  }
  const answer = await vscode.window.showWarningMessage(
    title,
    { modal: true, detail },
    goLabel,
  );
  return answer === goLabel;
}
