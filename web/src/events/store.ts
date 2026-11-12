import { useCallback, useEffect, useReducer } from 'react'
import type { EventFeed } from './feed'
import { initial, reduce, type SlateState } from './reduce'

export type { Session, SlateState } from './reduce'

export function useSlateEvents(feed: EventFeed): SlateState & { reload: () => void; error: string | null } {
  const [state, dispatch] = useReducer(reduce, initial)
  const [error, setError] = useReducer((_: string | null, next: string | null) => next, null)

  const reload = useCallback(() => {
    feed.tasks().then(
      (details) => {
        setError(null)
        dispatch({ type: 'tasks.loaded', details })
      },
      (failure: Error) => setError(failure.message),
    )
  }, [feed])

  useEffect(() => {
    try {
      const close = feed.connect(dispatch)
      reload()
      return close
    } catch (failure) {
      setError((failure as Error).message)
      return undefined
    }
  }, [feed, reload])

  return { ...state, reload, error }
}
