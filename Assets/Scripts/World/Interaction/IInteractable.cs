namespace FarmCreatures.World.Interaction
{
    public interface IInteractable
    {
        string InteractionLabel { get; }
        void Interact(InteractionContext context);
    }
}
