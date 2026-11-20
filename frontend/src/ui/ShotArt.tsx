import { useState } from 'react';

export type ArtKind = 'lecture' | 'poster' | 'shoe' | 'chat' | 'image';

function ImagePreview({ src, alt }: { src: string; alt: string }) {
  const [failed, setFailed] = useState(false);
  return (
    <div className="shot-art shot-art--image" role="img" aria-label={failed ? `${alt}: unavailable` : alt}>
      {failed
        ? <p className="sg-preview-unavailable">Preview unavailable</p>
        : <img src={src} alt="" onError={() => setFailed(true)} />}
    </div>
  );
}

/**
 * Synthetic "screenshot" artwork for demo fixtures. Everything here is invented
 * sample content drawn with HTML/CSS; none of it is a real user screenshot.
 * Sizes use container query units so the art scales with its card.
 */
export function ShotArt({ kind, src, alt }: { kind: ArtKind; src?: string; alt?: string }) {
  if (kind === 'image' && src) return <ImagePreview key={src} src={src} alt={alt ?? 'Screenshot preview'} />;
  return (
    <div className={`shot-art shot-art--${kind}`} role="img" aria-label={alt ?? `Demo ${kind} screenshot`}>
      {kind === 'lecture' && (
        <div className="art-lecture">
          <p className="hand t">Math 33A — Lecture 5</p>
          <p>• Eigenvalues &amp; Eigenvectors</p>
          <p className="hand">Av = λv</p>
          <p>• det(A − λI) = 0</p>
          <p>
            • <u>diagonalizable</u> ⇔ n lin. indep. eigenvectors
          </p>
        </div>
      )}
      {kind === 'poster' && (
        <div className="art-poster">
          <div className="art-poster__text">
            <p className="art-poster__title">
              FALL
              <br />
              TECH MIXER
            </p>
            <p>▦ Thu, Nov 14</p>
            <p>◷ 6:00 – 8:30 PM</p>
            <p>⌖ Student Union, Ballroom A</p>
          </div>
          <div className="art-poster__tower" aria-hidden="true" />
        </div>
      )}
      {kind === 'shoe' && (
        <div className="art-shoe">
          <div className="art-shoe__img" aria-hidden="true">
            <span className="art-shoe__sole" />
            <span className="art-shoe__upper" />
            <span className="art-shoe__n">N</span>
          </div>
          <div className="art-shoe__info">
            <p>New Balance 574</p>
            <p className="art-shoe__price">
              <b>$89.99</b> <s>$99.99</s> <em>10% OFF</em>
            </p>
            <p className="art-shoe__size">Size</p>
            <p className="art-shoe__sel">8.5</p>
          </div>
        </div>
      )}
      {kind === 'chat' && (
        <div className="art-chat">
          <p className="bub l">That trip was honestly so much fun 😊</p>
          <p className="bub l">We should make it an annual thing!</p>
          <p className="bub r">Absolutely!! Same time next year? 👀</p>
        </div>
      )}
    </div>
  );
}
