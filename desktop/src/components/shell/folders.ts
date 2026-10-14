/** Choosing a folder: the native dialog in the app, a small in-page browser otherwise. */
import { native } from '../../lib/native';
import { useFolderPicker } from './FolderPicker';

export async function chooseFolder(start?: string): Promise<string | null> {
  const n = native();
  if (n) return n.pickFolder(start);
  return useFolderPicker.getState().pick(start);
}
