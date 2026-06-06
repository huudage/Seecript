import { Composition, registerRoot } from 'remotion'
import { PackagingTrack, packagingTrackSchema, defaultPackagingProps } from './PackagingTrack'
import { AnimatedImage, animationSpecSchema, defaultAnimationSpec } from './AnimatedImage'

const RemotionRoot: React.FC = () => {
  return (
    <>
      <Composition
        id="PackagingTrack"
        component={PackagingTrack}
        durationInFrames={30 * 30}
        fps={30}
        width={1080}
        height={1920}
        schema={packagingTrackSchema}
        defaultProps={defaultPackagingProps}
      />
      <Composition
        id="AnimatedImage"
        component={AnimatedImage}
        durationInFrames={30 * 4}
        fps={30}
        width={1080}
        height={1920}
        schema={animationSpecSchema}
        defaultProps={defaultAnimationSpec}
        calculateMetadata={({ props, defaultProps }) => {
          const fps = 30
          const dur = props?.duration_seconds ?? defaultProps?.duration_seconds ?? 4
          return {
            durationInFrames: Math.max(1, Math.round(dur * fps)),
            fps,
          }
        }}
      />
    </>
  )
}

registerRoot(RemotionRoot)
