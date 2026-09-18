"use client";
import { useRef, useState } from "react";

/** For credential-bearing requests: arguments/results never enter a shared cache. */
export function useTransientMutation<Variables, Result, Context = undefined>(options: {
  mutationFn: (variables: Variables) => Promise<Result>;
  onMutate?: (variables: Variables) => Context;
  onSuccess?: (result: Result, variables: Variables, context: Context | undefined) => void;
  onError?: (error: unknown, variables: Variables, context: Context | undefined) => void;
}) {
  const [isPending, setPending] = useState(false);
  const pending = useRef(false);
  const mutateAsync = async (variables: Variables): Promise<Result> => {
    if (pending.current) throw new Error("A request is already being submitted.");
    pending.current = true; setPending(true);
    let context: Context | undefined;
    try { context = options.onMutate?.(variables); const result = await options.mutationFn(variables); options.onSuccess?.(result, variables, context); return result; }
    catch (error) { options.onError?.(error, variables, context); throw error; }
    finally { pending.current = false; setPending(false); }
  };
  const mutate = (variables: Variables) => { void mutateAsync(variables).catch(() => { /* Errors belong to onError or the caller of mutateAsync. */ }); };
  return { mutate, mutateAsync, isPending };
}
