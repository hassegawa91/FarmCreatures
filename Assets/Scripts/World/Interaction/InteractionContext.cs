using UnityEngine;

namespace FarmCreatures.World.Interaction
{
    public struct InteractionContext
    {
        public GameObject actor;
        public Vector2 actorFacingDirection;

        public InteractionContext(GameObject actor, Vector2 actorFacingDirection)
        {
            this.actor = actor;
            this.actorFacingDirection = actorFacingDirection;
        }
    }
}
