import { Composition, registerRoot } from 'remotion'
import { PackagingTrack, packagingTrackSchema, defaultPackagingProps } from './PackagingTrack'

const RemotionRoot: React.FC = () => {
  return (
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
  )
}

registerRoot(RemotionRoot)
