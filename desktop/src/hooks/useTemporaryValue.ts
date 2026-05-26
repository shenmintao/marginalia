import { useCallback, useEffect, useRef, useState } from "react";

/** A value that auto-reverts to `defaultValue` after `duration` ms.
 *  Cleaner than ad-hoc `setTimeout` chains in copy buttons / toasts.
 *
 *  Borrowed from cherry-studio's hook of the same name. */
export function useTemporaryValue<T>(defaultValue: T, duration = 2000) {
  const [value, setValue] = useState<T>(defaultValue);
  const timeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const set = useCallback(
    (next: T) => {
      if (timeoutRef.current) clearTimeout(timeoutRef.current);
      setValue(next);
      if (next !== defaultValue) {
        timeoutRef.current = setTimeout(() => {
          setValue(defaultValue);
          timeoutRef.current = null;
        }, duration);
      }
    },
    [defaultValue, duration],
  );

  useEffect(() => () => {
    if (timeoutRef.current) clearTimeout(timeoutRef.current);
  }, []);

  return [value, set] as const;
}
