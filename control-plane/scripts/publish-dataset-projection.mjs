import { lstat, readFile, readdir, rename, rm } from "node:fs/promises";
import path from "node:path";

export async function publishDatasetProjection({
  catalogStaging,
  trackingStaging,
  output,
  trackingDirectory,
  replaceCatalog = rename,
}) {
  for (const [target, label, expectedType] of [
    [output, "catalog", "file"],
    [trackingDirectory, "tracking directory", "directory"],
  ]) {
    const info = await lstat(target);
    const validType = expectedType === "file" ? info.isFile() : info.isDirectory();
    if (info.isSymbolicLink() || !validType) {
      throw new Error(`dataset ${label} is not a regular publication target`);
    }
  }

  const trackingFiles = (await readdir(trackingStaging)).sort();
  const newTrackingFiles = [];
  for (const filename of trackingFiles) {
    const staged = path.join(trackingStaging, filename);
    const published = path.join(trackingDirectory, filename);
    try {
      const info = await lstat(published);
      if (!info.isFile() || info.isSymbolicLink() || !(await readFile(staged)).equals(await readFile(published))) {
        throw new Error(`dataset tracking asset conflicts with existing publication: ${filename}`);
      }
    } catch (error) {
      if (error?.code !== "ENOENT") throw error;
      newTrackingFiles.push(filename);
    }
  }

  const publishedTracking = [];
  try {
    for (const filename of newTrackingFiles) {
      await rename(path.join(trackingStaging, filename), path.join(trackingDirectory, filename));
      publishedTracking.push(filename);
    }
    await replaceCatalog(catalogStaging, output);
  } catch (error) {
    const rollback = await Promise.allSettled(
      publishedTracking.map((filename) => rm(path.join(trackingDirectory, filename))),
    );
    const rollbackFailure = rollback.find((result) => result.status === "rejected");
    if (rollbackFailure) {
      throw new AggregateError([error, rollbackFailure.reason], "dataset catalog publication and rollback failed");
    }
    throw error;
  }

  const currentTracking = new Set(trackingFiles);
  for (const filename of await readdir(trackingDirectory)) {
    if (!currentTracking.has(filename)) await rm(path.join(trackingDirectory, filename));
  }
}
